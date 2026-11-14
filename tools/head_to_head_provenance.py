"""Validate an explicit local-only V3 provenance sidecar, without inventing it.

This schema is a producer contract, not a writer or proof of record authenticity.
No current bundle is automatically upgraded to satisfy it. No transport exists.
"""

from __future__ import annotations

import re


class InvalidProvenance(ValueError):
    pass


def require(value):
    if not value:
        raise InvalidProvenance("missing or conflicting provenance evidence")


def exact(value, fields):
    require(type(value) is dict and set(value) == set(fields.split()))


def sha(value):
    return type(value) is str and re.fullmatch("[0-9a-f]{64}", value) is not None


def model(value):
    return (
        type(value) is str
        and re.fullmatch("[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}", value) is not None
    )


def validate(sidecar):
    exact(
        sidecar,
        "version storage run_id protocol_sha256 side game_id game_ordinal machine_slot deck_sha256 format code observation",
    )
    require(type(sidecar["version"]) is int and sidecar["version"] == 1)
    require(sidecar["storage"] == "local_only")
    for field in ("run_id", "protocol_sha256", "game_id", "deck_sha256"):
        require(sha(sidecar[field]))
    require(
        sidecar["side"] in ("A", "B") and sidecar["machine_slot"] in ("first", "second")
    )
    require(
        type(sidecar["game_ordinal"]) is int and 1 <= sidecar["game_ordinal"] <= 100000
    )
    require(
        sidecar["format"]
        in (
            "Standard",
            "Historic",
            "Brawl",
            "Timeless",
            "Alchemy",
            "Explorer",
            "Limited",
        )
    )
    code = sidecar["code"]
    exact(code, "kind start_sha256 end_sha256 dirty source_evidence_sha256")
    require(code["kind"] == "immutable_runtime_snapshot")
    require(code["dirty"] is False)
    require(
        all(
            sha(code[k])
            for k in ("start_sha256", "end_sha256", "source_evidence_sha256")
        )
    )
    require(code["start_sha256"] == code["end_sha256"])
    obs = sidecar["observation"]
    exact(obs, "configured_alias complete truncated source_evidence_sha256 calls")
    require(model(obs["configured_alias"]))
    require(
        obs["complete"] is True
        and obs["truncated"] is False
        and sha(obs["source_evidence_sha256"])
    )
    require(type(obs["calls"]) is list and len(obs["calls"]) <= 100000)
    seen = set()
    served = set()
    for call in obs["calls"]:
        exact(call, "request_id_sha256 status served_model")
        require(
            sha(call["request_id_sha256"]) and call["request_id_sha256"] not in seen
        )
        seen.add(call["request_id_sha256"])
        require(call["status"] in ("completed", "timeout", "cancelled", "error"))
        # A timeout with no response cannot be assigned the configured model.
        if call["served_model"] is None:
            require(call["status"] != "completed")
            continue
        require(model(call["served_model"]))
        served.add(call["served_model"])
    unknown = sum(call["served_model"] is None for call in obs["calls"])
    # Absence, mixed identities or unknown failed requests remain unqualified.
    qualified = bool(obs["calls"]) and len(served) == 1 and not unknown
    return {
        "contract_valid": True,
        "recorded_model_identity_consistent": qualified,
        "observed_request_count": len(obs["calls"]),
        "distinct_served_identity_count": len(served),
        "requests_without_served_identity": unknown,
        "configured_alias_matches_observed": bool(served)
        and served == {obs["configured_alias"]},
        "storage": "local_only",
        "authenticity": "not_verified_by_schema",
        "acceptance": "producer contract only; runtime capture and real twenty-game gate remain open",
    }
