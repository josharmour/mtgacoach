"""Join supplied local V3 evidence; consistency is NOT runtime authenticity.

No file writer, transport, collector or automatic upgrade of historical bundles.
The explicit snapshot/revision binding is an assertion, not attestation.
"""

from __future__ import annotations

import hashlib
import json

from tools import head_to_head as report
from tools import head_to_head_provenance as provenance


def protocol_digest(protocol):
    report.protocol(protocol)
    return hashlib.sha256(
        json.dumps(protocol, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def analyze(protocol, evidence):
    """Fail closed on inconsistent joins, return only the aggregate report.

    Caller supplies {record, sidecar, code_binding} per side/game. Even a
    consistent join cannot prove actual served identity, deployed bytes,
    metrics, local storage or honest provenance. No acceptance is awarded.
    """
    expected_digest = protocol_digest(protocol)
    report.require(type(evidence) is list)
    records, games, requests = [], set(), set()
    for item in evidence:
        provenance.exact(item, "record sidecar code_binding")
        row, sidecar, binding = item["record"], item["sidecar"], item["code_binding"]
        report.record(protocol, row)
        checked = provenance.validate(sidecar)
        report.require(checked["recorded_model_identity_consistent"])
        key = (row["game"], row["side"])
        report.require(key not in games)
        games.add(key)
        for field in ("run_id", "game_id", "side", "deck_sha256", "format"):
            report.require(sidecar[field] == row[field])
        report.require(sidecar["game_ordinal"] == row["game"])
        report.require(sidecar["protocol_sha256"] == expected_digest)
        slot = 0 if sidecar["machine_slot"] == "first" else 1
        report.require(protocol["machines"][slot] == row["machine"])
        provenance.exact(binding, "snapshot_sha256 code_revision source_evidence_sha256")
        report.require(provenance.sha(binding["snapshot_sha256"]))
        report.require(report.digest(binding["code_revision"], 40))
        report.require(provenance.sha(binding["source_evidence_sha256"]))
        report.require(binding["snapshot_sha256"] == sidecar["code"]["start_sha256"])
        report.require(binding["code_revision"] == row["code_revision"])
        report.require(binding["source_evidence_sha256"] == sidecar["code"]["source_evidence_sha256"])
        calls = sidecar["observation"]["calls"]
        report.require(row["metrics"]["model_calls"] == len(calls))
        for call in calls:
            report.require(call["served_model"] == row["model"])
            request = call["request_id_sha256"]
            report.require(request not in requests)
            requests.add(request)
        records.append(row)
    result = report.analyze(protocol, records)
    result["provenance"] = {
        "supplied_side_records_checked": len(records),
        "binding_consistency": "checked_only",
        "authenticity": "not_verified_by_schema_or_join",
        "runtime_capture": "not_established",
        "acceptance": "not_assessed; real twenty-game and runtime provenance gates remain open",
    }
    return result
