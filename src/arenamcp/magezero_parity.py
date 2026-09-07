"""Provenance-aware parity-fixture schema, loader and exact comparator (task13).

Defines the on-disk fixture contract for (Arena-shaped state, authoritative
XMage-emitted feature/action vector) pairs and an exact row-by-row comparator.
This module does NOT claim that any parity has been established; fixtures with
``emitter_kind == "synthetic_harness"`` are explicitly non-parity evidence and
cannot be used to assert encoder parity (see task13 contract in TASKS.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FIXTURE_SCHEMA_VERSION = "mz_parity_fixture_v1"

#: actionType ordinal names, decoded from ActionEncoder$ActionType bytecode.
ACTION_TYPES = ("PRIORITY", "CHOOSE_NUM", "BLANK", "CHOOSE_TARGET", "MAKE_CHOICE", "CHOOSE_USE")

REQUIRED_TOP_LEVEL = (
    "schema_version",
    "case_id",
    "arena_state",
    "emitted_rows",
    "provenance",
)
REQUIRED_PROVENANCE = (
    "emitter_kind",  # "xmage_jvm_hdf5" (authoritative) | "synthetic_harness" (non-parity)
    "encoder_version",
    "action_schema_version",
)
AUTHORITATIVE_EMITTER = "xmage_jvm_hdf5"
SYNTHETIC_EMITTER = "synthetic_harness"
ACTION_DIM = 128


class FixtureValidationError(Exception):
    """Raised when a fixture record violates the schema."""


def validate_record(record: dict[str, Any]) -> list[str]:
    """Return a list of schema problems (empty = valid)."""
    problems: list[str] = []
    for key in REQUIRED_TOP_LEVEL:
        if key not in record:
            problems.append(f"missing required field: {key}")
    prov = record.get("provenance") or {}
    for key in REQUIRED_PROVENANCE:
        if key not in prov:
            problems.append(f"missing provenance field: {key}")
    if prov.get("emitter_kind") not in (AUTHORITATIVE_EMITTER, SYNTHETIC_EMITTER):
        problems.append("provenance.emitter_kind must be 'xmage_jvm_hdf5' or 'synthetic_harness'")

    rows = record.get("emitted_rows")
    if not isinstance(rows, list) or not rows:
        problems.append("emitted_rows must be a non-empty list")
        return problems

    seen_row_ids: set[int] = set()
    case_had_priority = False
    for i, row in enumerate(rows):
        for key in ("row_index", "indices", "offsets", "row", "action_dim"):
            if key not in row:
                problems.append(f"emitted_rows[{i}] missing '{key}'")
                continue
        if row.get("action_dim") != ACTION_DIM:
            problems.append(f"emitted_rows[{i}] action_dim must be {ACTION_DIM}")
        idx = row.get("indices")
        offs = row.get("offsets")
        vec = row.get("row")
        at = row.get("action_type")
        if at is None or not isinstance(at, int) or not (0 <= at < len(ACTION_TYPES)):
            problems.append(
                f"emitted_rows[{i}] action_type must be an int in [0..{len(ACTION_TYPES) - 1}]"
            )
        if isinstance(idx, list) and any(
            not isinstance(v, int) or v < 0 for v in idx
        ):
            problems.append(f"emitted_rows[{i}] indices must be non-negative ints")
        if (
            isinstance(idx, list)
            and isinstance(offs, list)
            and idx
            and offs
            and offs[0] != 0
            or (isinstance(offs, list) and any(b < a for a, b in zip(offs, offs[1:])))
        ):
            problems.append(f"emitted_rows[{i}] offsets must start at 0 and be monotonic")
        if isinstance(idx, list) and isinstance(offs, list) and offs and offs[-1] != len(idx):
            problems.append(f"emitted_rows[{i}] offsets[-1] must equal len(indices)")
        if isinstance(vec, list) and isinstance(row.get("action_dim"), int):
            if len(vec) != row["action_dim"] + 4:
                problems.append(f"emitted_rows[{i}] row width must be action_dim + 4")
            for v in vec:
                if not isinstance(v, (int, float)):
                    problems.append(f"emitted_rows[{i}] row values must be numeric")
                    break
                if v != v or v in (float("inf"), -float("inf")):
                    problems.append(f"emitted_rows[{i}] row values must be finite")
                    break
        ip = row.get("is_player")
        if ip is not None and (not isinstance(ip, int) or ip not in (0, 1)):
            problems.append(f"emitted_rows[{i}] is_player must be 0 or 1")
        if at == 0:  # only PRIORITY rows count toward the completeness gate
            case_had_priority = True
        rid = row.get("row_index")
        if isinstance(rid, int):
            if rid in seen_row_ids:
                problems.append(f"emitted_rows[{i}] duplicate row_index {rid}")
            seen_row_ids.add(rid)
    if not case_had_priority:
        problems.append(
            "no PRIORITY (action_type=0) row present: row set is incomplete for parity comparison"
        )
    return problems


def load_fixture(path) -> list[dict[str, Any]]:
    """Load a JSONL parity fixture corpus; raises on any schema violation."""
    import json
    from pathlib import Path

    records: list[dict[str, Any]] = []
    with open(Path(path), "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise FixtureValidationError(f"{path}:{lineno}: invalid JSON: {e}") from e
            problems = validate_record(rec)
            if problems:
                raise FixtureValidationError(f"{path}:{lineno}: " + "; ".join(problems))
            records.append(rec)
    if not records:
        raise FixtureValidationError(f"{path}: empty fixture corpus")
    return records


def is_authoritative(record: dict[str, Any]) -> bool:
    return record.get("provenance", {}).get("emitter_kind") == AUTHORITATIVE_EMITTER


# ---------------------------------------------------------------------------
# Exact comparator (bit-exact; never derives vectors from the encoder under test)
# ---------------------------------------------------------------------------


@dataclass
class RowComparison:
    row_index: int
    indices_equal: bool
    policy_equal: bool
    extras_equal: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class ParityReport:
    case_id: str
    per_row: list[RowComparison]
    authoritative: bool

    @property
    def all_equal(self) -> bool:
        return all(
            r.indices_equal and r.policy_equal and r.extras_equal for r in self.per_row
        )

    @property
    def indices_equal(self) -> bool:
        return all(r.indices_equal for r in self.per_row)

    @property
    def policy_equal(self) -> bool:
        return all(r.policy_equal for r in self.per_row)

    @property
    def extras_equal(self) -> bool:
        return all(r.extras_equal for r in self.per_row)


def _bits_equal(a: float, b: float) -> bool:
    """Bit-exact float32 comparison; NaN==NaN and -0.0==0.0 for this purpose."""
    import math
    import struct

    def key(x: float) -> bytes:
        x32 = struct.unpack("<f", struct.pack("<f", x))[0]
        if x32 == 0.0:
            x32 = 0.0  # collapse -0.0
        if math.isnan(x32):
            x32 = float("nan")  # collapse NaN payloads
        return struct.pack("<f", x32)

    return key(a) == key(b)


def compare_row(expected: dict[str, Any], actual: dict[str, Any]) -> RowComparison:
    reasons: list[str] = []
    exp_idx_sorted = sorted(int(v) for v in expected["indices"])
    act_idx_sorted = sorted(int(v) for v in actual["indices"])
    indices_equal = exp_idx_sorted == act_idx_sorted
    if not indices_equal:
        only_exp = sorted(set(exp_idx_sorted) - set(act_idx_sorted))[:8]
        only_act = sorted(set(act_idx_sorted) - set(exp_idx_sorted))[:8]
        reasons.append(f"index sets differ: expected-only={only_exp} actual-only={only_act}")

    ad = int(expected["action_dim"])
    exp_vec = [float(v) for v in expected["row"]]
    act_vec = [float(v) for v in actual["row"]]
    policy_equal = len(exp_vec) >= ad and len(act_vec) >= ad and all(
        _bits_equal(e, a) for e, a in zip(exp_vec[:ad], act_vec[:ad])
    )
    if not policy_equal:
        bad = [k for k in range(ad) if not _bits_equal(exp_vec[k], act_vec[k])][:8]
        reasons.append(f"policy slots differ at {bad}")

    extras_equal = True
    if len(exp_vec) >= ad + 4 and len(act_vec) >= ad + 4:
        labels = ("resultLabel", "stateScore", "isPlayer", "actionType")
        for off, name in enumerate(labels):
            e, a = exp_vec[ad + off], act_vec[ad + off]
            if not _bits_equal(e, a):
                extras_equal = False
                reasons.append(f"{name} differs: expected {e!r} actual {a!r}")
    return RowComparison(
        row_index=int(expected.get("row_index", -1)),
        indices_equal=indices_equal,
        policy_equal=policy_equal,
        extras_equal=extras_equal,
        reasons=reasons,
    )


def compare_case(record: dict[str, Any], actual_rows: list[dict[str, Any]]) -> ParityReport:
    """Compare one fixture case's authoritative rows against actually-emitted rows.

    ``actual_rows`` must come from the authoritative emitter (JUnit-captured
    LabeledStateWriter output against the same Arena-shaped state), never from
    the Python encoder under test.
    """
    if not isinstance(actual_rows, list) or not actual_rows:
        raise FixtureValidationError(
            f"case {record.get('case_id')}: actual_rows must be a non-empty list"
        )
    expected_rows = sorted(record["emitted_rows"], key=lambda r: r["row_index"])
    actual_sorted = sorted(actual_rows, key=lambda r: r["row_index"])
    if len(expected_rows) != len(actual_sorted):
        raise FixtureValidationError(
            f"case {record.get('case_id')}: row count differs "
            f"(expected {len(expected_rows)}, actual {len(actual_sorted)})"
        )
    seen: set[int] = set()
    for r in actual_sorted:
        rid = r.get("row_index")
        if not isinstance(rid, int):
            raise FixtureValidationError(
                f"case {record.get('case_id')}: actual row missing integer row_index"
            )
        if rid in seen:
            raise FixtureValidationError(
                f"case {record.get('case_id')}: duplicate actual row_index {rid}"
            )
        seen.add(rid)
    per_row = [compare_row(e, a) for e, a in zip(expected_rows, actual_sorted)]
    return ParityReport(
        case_id=record.get("case_id", "?"),
        per_row=per_row,
        authoritative=is_authoritative(record),
    )


# ---------------------------------------------------------------------------
# Provenance gating — synthetic data can never pass as authoritative parity
# ---------------------------------------------------------------------------


def assert_parity_evidence(report: ParityReport) -> None:
    """Assert that a ParityReport may be used to claim parity.

    Raises unless the fixture is authoritative AND every row is exactly equal.
    Synthetic harness data must fail here even when perfectly self-consistent,
    because it cannot establish product parity.
    """
    if not report.authoritative:
        raise FixtureValidationError(
            f"case {report.case_id}: fixture provenance is not "
            f"'{AUTHORITATIVE_EMITTER}'; synthetic harness data cannot establish parity"
        )
    if not report.all_equal:
        failed = [r.row_index for r in report.per_row if not (r.indices_equal and r.policy_equal and r.extras_equal)]
        raise FixtureValidationError(
            f"case {report.case_id}: exact parity FAILED for rows {failed}"
        )
