"""Invented fixtures test joins, not real games or authenticated provenance."""

import copy
import hashlib

import pytest
from tests.test_head_to_head import setup
from tools import head_to_head_ingest as ingest
from tools.head_to_head import InvalidRun
from tools.head_to_head_provenance import InvalidProvenance


def fixture():
    protocol, rows = setup()
    evidence = []
    for row in rows:
        calls = [
            dict(
                request_id_sha256=hashlib.sha256(f"{row['game']}/{row['side']}/{i}".encode()).hexdigest(),
                status="completed",
                served_model=row["model"],
            )
            for i in range(row["metrics"]["model_calls"])
        ]
        sidecar = dict(
            version=1,
            storage="local_only",
            run_id=row["run_id"],
            protocol_sha256=ingest.protocol_digest(protocol),
            side=row["side"],
            game_id=row["game_id"],
            game_ordinal=row["game"],
            machine_slot="first" if row["machine"] == protocol["machines"][0] else "second",
            deck_sha256=row["deck_sha256"],
            format=row["format"],
            code=dict(
                kind="immutable_runtime_snapshot",
                start_sha256="a" * 64,
                end_sha256="a" * 64,
                dirty=False,
                source_evidence_sha256="b" * 64,
            ),
            observation=dict(
                configured_alias="current",
                complete=True,
                truncated=False,
                source_evidence_sha256="c" * 64,
                calls=calls,
            ),
        )
        evidence.append(
            dict(
                record=row,
                sidecar=sidecar,
                code_binding=dict(
                    snapshot_sha256="a" * 64,
                    code_revision=row["code_revision"],
                    source_evidence_sha256="b" * 64,
                ),
            )
        )
    return protocol, evidence


def test_consistent_synthetic_join_is_not_authenticated_and_never_mutates():
    protocol, evidence = fixture()
    before = copy.deepcopy((protocol, evidence))
    out = ingest.analyze(protocol, evidence)
    assert out["paired_games"] == 2
    assert out["provenance"]["authenticity"] == "not_verified_by_schema_or_join"
    assert "not_assessed" in out["provenance"]["acceptance"]
    assert (protocol, evidence) == before
    assert protocol["run_id"] not in str(out)
    assert "request_id" not in str(out)


@pytest.mark.parametrize(
    "path,value",
    [
        (("sidecar", "protocol_sha256"), "0" * 64),
        (("sidecar", "game_id"), "0" * 64),
        (("sidecar", "game_ordinal"), 2),
        (("sidecar", "side"), "B"),
        (("sidecar", "machine_slot"), "second"),
        (("sidecar", "deck_sha256"), "0" * 64),
        (("sidecar", "run_id"), "0" * 64),
        (("sidecar", "format"), "Brawl"),
        (("code_binding", "snapshot_sha256"), "0" * 64),
        (("code_binding", "code_revision"), "0" * 40),
        (("code_binding", "source_evidence_sha256"), "0" * 64),
    ],
)
def test_conflicting_join_refuses(path, value):
    protocol, evidence = fixture()
    evidence[0][path[0]][path[1]] = value
    with pytest.raises((InvalidRun, InvalidProvenance)):
        ingest.analyze(protocol, evidence)


@pytest.mark.parametrize("kind", ["missing", "duplicate", "request_reuse", "drift", "unknown", "count"])
def test_missing_duplicate_and_model_evidence_refuses(kind):
    protocol, evidence = fixture()
    calls = evidence[0]["sidecar"]["observation"]["calls"]
    if kind == "missing":
        del evidence[0]["sidecar"]
    elif kind == "duplicate":
        evidence.append(copy.deepcopy(evidence[0]))
    elif kind == "request_reuse":
        evidence[1]["sidecar"]["observation"]["calls"][0]["request_id_sha256"] = calls[0]["request_id_sha256"]
    elif kind == "drift":
        calls[0]["served_model"] = "other"
    elif kind == "unknown":
        calls[0].update(status="timeout", served_model=None)
    elif kind == "count":
        calls.pop()
    with pytest.raises((InvalidRun, InvalidProvenance)):
        ingest.analyze(protocol, evidence)


def test_unpaired_game_stays_missing():
    protocol, evidence = fixture()
    out = ingest.analyze(protocol, evidence[:1])
    assert out["paired_games"] == 0
    assert out["missing_or_unpaired_games"] == 2
