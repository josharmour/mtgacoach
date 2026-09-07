#!/usr/bin/env python3
"""Read-only tooling for task13 parity-fixture collection (NO parity claims).

Two independent capabilities:

1. ``xmage-hdf5-inspect`` — inspect existing recorded training HDF5 shards
   (READ ONLY) and report their exact on-disk layout and per-row contents.
   Layout contract verified from bytecode of
   ``org.mage.magezero.LabeledStateWriter.writeRecord`` (mage-magezero-1.4.58.jar):
     /indices int32 [nnz] (sorted unique per row), /offsets int64 [N+1],
     /row float32 [N, A+4] = [policy(A) | resultLabel | stateScore | isPlayer |
     actionType.ordinal].
   This PROVES an XML/HDF5 row layout report is faithful to the writer, but an
   HDF5 row is NOT a matched Arena-state fixture and never establishes
   Arena↔XMage encoder parity by itself.

2. ``validate-corpus`` — validate a JSONL parity-fixture corpus against the
   provenance-aware schema in :mod:`arenamcp.magezero_parity`. Authoritative
   records must carry ``emitter_kind == "xmage_jvm_hdf5"`` plus encoder/action
   schema versions and artifact hashes; synthetic harness records are allowed
   but can never satisfy parity gating.

Never launches a JVM, never writes to ~/repos, never contacts the fleet.
Requires numpy (present) and, for command 1 only, h5py — absent in the current
tooling venv, in which case command 1 reports the exact missing dependency.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from arenamcp.magezero_parity import (  # noqa: E402
    ACTION_TYPES,
    AUTHORITATIVE_EMITTER,
    FIXTURE_SCHEMA_VERSION,
    FixtureValidationError,
    load_fixture,
)


def cmd_xmage_hdf5_inspect(paths: list[Path], max_rows: int) -> int:
    try:
        import h5py  # noqa: F401
        import numpy as np  # noqa: F401
    except ModuleNotFoundError as e:
        print(f"BLOCKED: missing dependency for HDF5 inspection: {e}")
        print("Layout contract (decoded from LabeledStateWriter bytecode) documented in module docstring.")
        return 2
    import numpy as np

    for path in paths:
        print(f"== {path}")
        with h5py.File(path, "r") as f:  # read-only
            idx = f["/indices"][...] if "/indices" in f else None
            off = f["/offsets"][...] if "/offsets" in f else None
            row = f["/row"][...] if "/row" in f else None
            if row is None or off is None:
                print("  MISSING /row or /offsets datasets")
                return 2
            n = int(off.shape[0] - 1)
            nnz = int(off[-1])
            a_dim = int(row.shape[1] - 4)
            print(
                f"  rows={n} nnz={nnz} action_dim={a_dim} row_dtype={row.dtype} "
                f"idx_dtype={idx.dtype if idx is not None else None}"
            )
            shown = 0
            for i in range(n):
                a, b = int(off[i]), int(off[i + 1])
                if b <= a:
                    continue
                r = row[i]
                print(
                    f"  row{i}: nnz={b - a} actionType={int(r[a_dim + 3])} "
                    f"({ACTION_TYPES[int(r[a_dim + 3])] if 0 <= int(r[a_dim + 3]) < len(ACTION_TYPES) else '?'}) "
                    f"isPlayer={float(r[a_dim + 2]):.0f} stateScore={float(r[a_dim + 1]):.4f} "
                    f"resultLabel={float(r[a_dim]):.4f} first_indices={list(map(int, idx[a:b]))[:8]}"
                )
                shown += 1
                if shown >= max_rows:
                    break
    return 0


def cmd_validate_corpus(path: Path) -> int:
    try:
        records = load_fixture(path)
    except (FixtureValidationError, OSError, json.JSONDecodeError) as e:
        print(f"INVALID: {e}")
        return 2
    auth = sum(1 for r in records if r["provenance"]["emitter_kind"] == AUTHORITATIVE_EMITTER)
    synthetic = len(records) - auth
    case_ids = [r["case_id"] for r in records]
    dupes = sorted({c for c in case_ids if case_ids.count(c) > 1})
    print(f"schema_version={FIXTURE_SCHEMA_VERSION}")
    print(f"records={len(records)} authoritative={auth} synthetic_harness={synthetic}")
    print(f"cases={sorted(set(case_ids))}")
    print(f"duplicate_case_ids={dupes}")
    if auth == 0:
        print(
            "NOTE: zero authoritative records — this corpus contains NO parity evidence. "
            f"Authoritative records require emitter_kind='{AUTHORITATIVE_EMITTER}', "
            "XMage JVM artifact/version hashes and parser version."
        )
        return 0  # valid schema, but explicitly not parity evidence
    print(f"authoritative records present ({auth}); parity claim requires per-case exact comparison runs")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("xmage-hdf5-inspect", help="READ-ONLY inspect recorded XMage HDF5 shards")
    p1.add_argument("paths", nargs="+")
    p1.add_argument("--max-rows", type=int, default=5)
    p2 = sub.add_parser("validate-corpus", help="validate a JSONL parity fixture corpus")
    p2.add_argument("path")
    args = ap.parse_args(argv)
    if args.cmd == "xmage-hdf5-inspect":
        return cmd_xmage_hdf5_inspect([Path(p) for p in args.paths], args.max_rows)
    return cmd_validate_corpus(Path(args.path))


if __name__ == "__main__":
    sys.exit(main())
