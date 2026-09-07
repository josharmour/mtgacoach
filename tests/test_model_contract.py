"""Contract v2 model manifest parsing (task 04 / ws04).

The v2 contract (amendments from the checkpoint-1 review, applied to the live
runner/server evidence) separates the overloaded ``gate_threshold`` into a
selection-side ``deck_similarity_threshold`` and a promotion-side
``promotion_win_rate_threshold``, adds deck counts/hash, checkpoint identity,
encoder/action-schema versions, honest value-target semantics, and gated
certification evidence. Certification is structural: a manifest claiming
``certified`` without a complete evidence block cannot load.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

import pytest

from arenamcp.model_zoo import ManifestError, ModelSpec


_UWTEMPO_MANIFEST_V2: dict[str, Any] = {
    "schema_version": 2,
    "model_id": "UWTempo/ver2",
    "human_name": "UWTempo/ver2",
    "gen": 7,
    "version": 2,
    "deck": {
        "name": "UWTempo",
        "deck_counts": {
            "Island": 7,
            "Plains": 4,
            "Malcolm, Alluring Scoundrel": 4,
            "Spyglass Siren": 4,
            "Faerie Mastermind": 4,
            "Spectral Sailor": 4,
            "Skrelv, Defector Mite": 2,
            "Spell Pierce": 4,
            "Make Disappear": 4,
            "Fading Hope": 4,
            "Ossification": 4,
            "Protect the Negotiators": 2,
            "Seachrome Coast": 4,
            "Adarkar Wastes": 4,
            "Deserted Beach": 3,
            "Eiganjo, Seat of the Empire": 1,
            "Otawara, Soaring City": 1,
        },
        "size": 60,
        "singleton": False,
        "commander": None,
    },
    "format": {"family": "constructed", "variant": "standard"},
    "gate": {
        "deck_similarity_threshold": 0.60,
        "promotion_win_rate_threshold": 0.50,
    },
    "checkpoint_hash": "cea84411e8761b4ec081cfe9ddfc1a1ce10ab7450b20cf78bd30714f159446a2",
    "encoder_version": "enc-test-v1",
    "action_schema_version": "act-test-v1",
    "value_target": {
        "kind": "search_blended",
        "perspective": "actor",
        "range": [-1.0, 1.0],
        "terminal_handling": "win=1 loss=-1 draw=0",
        "truncation_handling": "bootstrapped_at_cap",
        "search_blend": {"strength": 0.35, "source": "mcts_visits"},
    },
    "promotion_status": "uncertified",
    "certification": None,
    "capabilities": {"warm": False},
    "protocol_version": 2,
    "trained_at": "2026-09-05T00:52:09.283983",
}


def _canonical_deck_counts(counts: dict[str, int]) -> str:
    """Mirror of model_zoo._canonical_deck_hash (kept local to assert the
    module's behavior matches the documented normalization)."""
    normalized: list[list[Any]] = []
    for name, count in counts.items():
        n = unicodedata.normalize("NFC", str(name)).casefold()
        n = " ".join(n.split())
        c = max(0, int(count))
        if c > 0 and n:
            normalized.append([n, c])
    normalized.sort()
    payload = json.dumps({"cards": normalized}, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TestManifestParsing:
    def test_v2_manifest_roundtrip(self) -> None:
        spec = ModelSpec.from_manifest(_UWTEMPO_MANIFEST_V2)
        assert spec.model_id == "UWTempo/ver2"
        assert spec.deck_similarity_threshold == 0.60
        assert spec.promotion_win_rate_threshold == 0.50
        assert spec.checkpoint_hash == "cea84411e8761b4ec081cfe9ddfc1a1ce10ab7450b20cf78bd30714f159446a2"
        assert spec.promotion_status == "uncertified"
        assert spec.capabilities["warm"] is False
        assert spec.deck_counts["Island"] == 7
        assert spec.deck_size == 60

    def test_legacy_v1_manifest_rejected_explicitly(self) -> None:
        v1 = dict(_UWTEMPO_MANIFEST_V2)
        v1.pop("schema_version")
        with pytest.raises(ManifestError, match="schema_version"):
            ModelSpec.from_manifest(v1)

    def test_certified_without_evidence_rejected(self) -> None:
        bad = dict(_UWTEMPO_MANIFEST_V2)
        bad["promotion_status"] = "certified"
        bad["certification"] = None
        with pytest.raises(ManifestError, match="certification"):
            ModelSpec.from_manifest(bad)

    def test_certified_with_partial_evidence_rejected(self) -> None:
        bad = dict(_UWTEMPO_MANIFEST_V2)
        bad["promotion_status"] = "certified"
        bad["certification"] = {"evaluated_at": "2026-09-06", "criteria_version": "v1"}
        with pytest.raises(ManifestError, match="certification"):
            ModelSpec.from_manifest(bad)

    def test_certified_with_complete_evidence_accepted(self) -> None:
        ok = dict(_UWTEMPO_MANIFEST_V2)
        ok["promotion_status"] = "certified"
        ok["certification"] = {
            "evaluated_at": "2026-09-06T00:00:00",
            "criteria_version": "panel-10deck-50pct-v1",
            "panel": {"decks": ["MonoR", "MonoU"], "games": 100,
                      "wins": 55, "losses": 42, "draws": 3},
            "aggregation": "mean_win_rate",
            "threshold": 0.50,
        }
        spec = ModelSpec.from_manifest(ok)
        assert spec.promotion_status == "certified"

    def test_uncertified_may_omit_certification(self) -> None:
        spec = ModelSpec.from_manifest(_UWTEMPO_MANIFEST_V2)
        assert spec.promotion_status == "uncertified"

    def test_rejected_status_preserved_not_upgraded(self) -> None:
        bad = dict(_UWTEMPO_MANIFEST_V2)
        bad["promotion_status"] = "rejected"
        spec = ModelSpec.from_manifest(bad)
        assert spec.promotion_status == "rejected"


class TestThresholdSeparation:
    def test_overloaded_gate_threshold_rejected_in_v2(self) -> None:
        bad = dict(_UWTEMPO_MANIFEST_V2)
        bad["gate_threshold"] = 0.55
        with pytest.raises(ManifestError, match="gate_threshold"):
            ModelSpec.from_manifest(bad)

    def test_missing_gate_block_rejected(self) -> None:
        bad = dict(_UWTEMPO_MANIFEST_V2)
        bad.pop("gate")
        with pytest.raises(ManifestError, match="gate"):
            ModelSpec.from_manifest(bad)


class TestDeckHashing:
    def test_real_training_deck_hash_verifies(self) -> None:
        """Canonical hash of the actual training deck (counts from the real
        UWTempo/ver2 manifest) must equal the deck_hash recorded / derivable."""
        counts = dict(_UWTEMPO_MANIFEST_V2["deck"]["deck_counts"])
        assert sum(counts.values()) == 60
        spec = ModelSpec.from_manifest(_UWTEMPO_MANIFEST_V2)
        assert spec.deck_hash == _canonical_deck_counts(counts)
        assert len(spec.deck_hash) == 64

    def test_hash_is_order_and_whitespace_stable(self) -> None:
        a = _canonical_deck_counts({"Island": 7, "Plains": 4})
        b = _canonical_deck_counts({"Plains": 4, "Island": 7})
        c = _canonical_deck_counts({"Island ": 7, "Plains": 4})
        assert a == b == c

    def test_zero_counts_dropped(self) -> None:
        assert _canonical_deck_counts({"Island": 0, "Plains": 4}) == _canonical_deck_counts({"Plains": 4})

    def test_hash_sensitive_to_count_changes(self) -> None:
        assert _canonical_deck_counts({"Island": 7}) != _canonical_deck_counts({"Island": 6})

    def test_manifest_deck_hash_mismatch_rejected(self) -> None:
        import copy
        bad = copy.deepcopy(_UWTEMPO_MANIFEST_V2)
        bad["deck"]["deck_hash"] = "0" * 64
        with pytest.raises(ManifestError, match="deck_hash"):
            ModelSpec.from_manifest(bad)


class TestIdentity:
    def test_checkpoint_hash_is_immutable_identity_seed(self) -> None:
        spec = ModelSpec.from_manifest(_UWTEMPO_MANIFEST_V2)
        assert spec.checkpoint_hash != spec.model_id
        assert len(spec.checkpoint_hash) == 64

    def test_missing_checkpoint_hash_rejected(self) -> None:
        bad = dict(_UWTEMPO_MANIFEST_V2)
        bad.pop("checkpoint_hash")
        with pytest.raises(ManifestError, match="checkpoint_hash"):
            ModelSpec.from_manifest(bad)

    def test_missing_deck_counts_rejected(self) -> None:
        bad = dict(_UWTEMPO_MANIFEST_V2)
        bad["deck"] = {"name": "UWTempo"}
        with pytest.raises(ManifestError, match="deck_counts"):
            ModelSpec.from_manifest(bad)


class TestCapabilities:
    def test_warm_explicitly_false(self) -> None:
        spec = ModelSpec.from_manifest(_UWTEMPO_MANIFEST_V2)
        assert spec.capabilities.get("warm") is False

    def test_missing_capabilities_defaults_to_unsupported(self) -> None:
        bad = dict(_UWTEMPO_MANIFEST_V2)
        bad.pop("capabilities")
        spec = ModelSpec.from_manifest(bad)
        assert spec.capabilities.get("warm") is False

    def test_warm_true_without_implementation_is_advertised_only(self) -> None:
        """Capabilities are declarative: a manifest may claim warm=True, but the
        client (task 05) must still refuse to send warm requests unless the
        server's protocol advertises it — never silently optimistic."""
        data = dict(_UWTEMPO_MANIFEST_V2)
        data["capabilities"] = {"warm": True}
        spec = ModelSpec.from_manifest(data)
        assert spec.capabilities["warm"] is True
