"""Synthetic contract tests are not captured runtime provenance."""

import copy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "p", Path(__file__).parents[1] / "tools/head_to_head_provenance.py"
)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def sample():
    return dict(
        version=1,
        storage="local_only",
        run_id="a" * 64,
        protocol_sha256="b" * 64,
        side="A",
        game_id="c" * 64,
        game_ordinal=1,
        machine_slot="first",
        deck_sha256="d" * 64,
        format="Standard",
        code=dict(
            kind="immutable_runtime_snapshot",
            start_sha256="e" * 64,
            end_sha256="e" * 64,
            dirty=False,
            source_evidence_sha256="f" * 64,
        ),
        observation=dict(
            configured_alias="current",
            complete=True,
            truncated=False,
            source_evidence_sha256="0" * 64,
            calls=[
                dict(
                    request_id_sha256="1" * 64,
                    status="completed",
                    served_model="observed-model",
                )
            ],
        ),
    )


def test_alias_never_substitutes_for_served_model():
    out = p.validate(sample())
    assert out["recorded_model_identity_consistent"]
    assert not out["configured_alias_matches_observed"]
    assert out["authenticity"] == "not_verified_by_schema"
    assert "observed-model" not in str(out)


@pytest.mark.parametrize(
    "calls",
    [
        [],
        [dict(request_id_sha256="1" * 64, status="timeout", served_model=None)],
        [
            dict(request_id_sha256="1" * 64, status="completed", served_model="a"),
            dict(request_id_sha256="2" * 64, status="completed", served_model="b"),
        ],
    ],
)
def test_missing_mixed_unknown_never_qualifies(calls):
    s = sample()
    s["observation"]["calls"] = calls
    assert not p.validate(s)["recorded_model_identity_consistent"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(storage="upload"),
        lambda s: s.update(customer="Private Person"),
        lambda s: s["code"].update(dirty=True),
        lambda s: s["code"].update(kind="git_head"),
        lambda s: s["code"].update(end_sha256="0" * 64),
        lambda s: s["observation"].update(truncated=True),
        lambda s: s["observation"].update(complete=False),
        lambda s: s["observation"]["calls"][0].update(served_model=None),
        lambda s: s["observation"]["calls"].append(copy.deepcopy(s["observation"]["calls"][0])),
    ],
)
def test_incomplete_conflicting_or_shared_evidence_rejects(mutate):
    s = sample()
    mutate(s)
    with pytest.raises(p.InvalidProvenance):
        p.validate(s)
