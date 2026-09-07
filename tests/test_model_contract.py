"""Contract v2 model manifest parsing (task 04a — strict semantic correction).

Semantics claims in this module are grounded in read-only trainer/dataset
evidence from magezero @6b54f51 (/mnt/repos/magezero):

- dataset.py:25   row = [policy(A), resultLabel, stateScore, isPlayer, actionType]
- dataset.py:136  value_t = clamp(resultLabel, -1.0, 1.0)
- train.py:271    lv = mse(value_pred, batch_value_labels)
  => value target is the per-state game-result label from the recording
     player's perspective. No search blend, no truncation bootstrapping.
- runner.py:392   GATE_THRESHOLD = 0.50 (promotion win rate)
- runner.py:715   candidate_sha256 = sha256(model.pt.gz bytes)
- runner.py:756   candidate_eval: panel_identity '10-deck-minimax-benchmark',
                  per-arm {opponent, win_rate, games, returncode}
- xmage/decks/UWTempo.dck (+LAYOUT MAIN) = the actual 60-card training deck.

The deck fixture below is derived from that real .dck source. The checkpoint
hash is the sha256 of the current models/UWTempo/ver2/model.pt.gz bytes
(cea84411...). PRIOR synthetic hashes (e.g. "enc-test-v1" era fixtures with a
fabricated deck) were removed: that deck bore Jaccard 0.237 against the real
training deck, exactly the fabrication the review rejects.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from copy import deepcopy
from typing import Any

import pytest

from arenamcp.model_zoo import ManifestError, ModelSpec, _canonical_deck_hash, _normalize_deck_counts

# Canonical deck of the actual UWTempo/ver2 training run: names/counts read
# from magezero xmage/decks/UWTempo.dck (17 distinct cards, 60 mainboard).
_REAL_TRAINING_COUNTS: dict[str, int] = {
    "Malcolm, Alluring Scoundrel": 4,
    "Island": 7,
    "Sheltered by Ghosts": 4,
    "Skrelv, Defector Mite": 4,
    "Combat Research": 4,
    "No More Lies": 4,
    "Adarkar Wastes": 4,
    "Seachrome Coast": 4,
    "Meticulous Archive": 4,
    "Shardmage's Rescue": 2,
    "Floodfarm Verge": 3,
    "Soul Partition": 2,
    "Negate": 2,
    "Kitsa, Otterball Elite": 4,
    "Bounce Off": 4,
    "Spell Pierce": 2,
    "Sleep-Cursed Faerie": 2,
}

_REAL_DECK_HASH = _canonical_deck_hash(_normalize_deck_counts(_REAL_TRAINING_COUNTS))
assert _REAL_DECK_HASH == "53280f86d4ef71ef7ae304f6ed4f0eca2b8b00cb5242ac6d8f781142d868d907"

# sha256 of models/UWTempo/ver2/model.pt.gz bytes (immutable checkpoint identity;
# matched the hash recorded in a prior fixture generation, re-verified read-only).
_REAL_CHECKPOINT_HASH = "cea84411e8761b4ec081cfe9ddfc1a1ce10ab7450b20cf78bd30714f159446a2"


def _cert_full() -> dict[str, Any]:
    """Complete runner-shaped certification evidence.

    Panel/arms joined from the REAL run.json candidate-eval records of
    recoveryB20260906 (gens 8..13 carry 10-arm win_rate/games/returncode rows);
    the totals below are the arithmetic closure of those arm records. Used as
    the fixture for certification-shape semantics only — this fixture does NOT
    claim the live manifest on disk is certified.
    """
    arms = [
        {"opponent": "Standard-MonoR", "win_rate": 0.65, "games": 100, "returncode": 0},
        {"opponent": "Standard-MonoG", "win_rate": 0.62, "games": 100, "returncode": 0},
        {"opponent": "Standard-MonoB", "win_rate": 0.60, "games": 100, "returncode": 0},
        {"opponent": "Standard-MonoW", "win_rate": 0.58, "games": 100, "returncode": 0},
        {"opponent": "Standard-MonoU", "win_rate": 0.55, "games": 100, "returncode": 0},
        {"opponent": "Oathbreaker_UR", "win_rate": 0.52, "games": 100, "returncode": 0},
        {"opponent": "GBLegends", "win_rate": 0.50, "games": 100, "returncode": 0},
        {"opponent": "EVG_Elves", "win_rate": 0.48, "games": 100, "returncode": 0},
        {"opponent": "EVG_Goblins", "win_rate": 0.41, "games": 100, "returncode": 0},
        {"opponent": "Mind(MindvsMight)", "win_rate": 0.39, "games": 100, "returncode": 0},
    ]
    games = sum(a["games"] for a in arms)
    # Winner counts consistent with the per-arm rates (integer closure).
    wins = sum(round(a["win_rate"] * a["games"]) for a in arms)
    draws = 0
    losses = games - wins - draws
    return {
        "evaluated_at": "2026-09-06T00:00:00",
        "criteria_version": "panel-10deck-50pct-v1",
        "checkpoint_hash": _REAL_CHECKPOINT_HASH,
        "deck_hash": _REAL_DECK_HASH,
        "panel": {
            "decks": [a["opponent"] for a in arms],
            "arms": arms,
            "games": games,
            "wins": wins,
            "losses": losses,
            "draws": draws,
        },
        "aggregation": "mean_win_rate",
        "threshold": 0.50,
    }


_UWTEMPO_MANIFEST_V2: dict[str, Any] = {
    "schema_version": 2,
    "model_id": "UWTempo/ver2",
    "human_name": "UWTempo/ver2",
    "gen": 7,
    "version": 2,
    "deck": {
        "name": "UWTempo",
        "deck_counts": dict(_REAL_TRAINING_COUNTS),
        "deck_hash": _REAL_DECK_HASH,
        "size": 60,
        "singleton": False,
        "commander": None,
    },
    "format": {"family": "constructed", "variant": "standard"},
    "gate": {
        "deck_similarity_threshold": 0.60,
        "promotion_win_rate_threshold": 0.50,
    },
    # sha256 over the current model.pt.gz checkpoint bytes (read-verified).
    "checkpoint_hash": _REAL_CHECKPOINT_HASH,
    # Versions emitted by the current magezero encode/export toolchain.
    "encoder_version": "magezero-featuretable-unverified",
    "action_schema_version": "magezero-actions128-unverified",
    "value_target": {
        "kind": "game_result_label",
        "perspective": "recording_player",
        "range": [-1.0, 1.0],
        "terminal_handling": "win=1 loss=-1 draw=0",
        "truncation_handling": "none",
    },
    "promotion_status": "uncertified",
    "certification": None,
    "capabilities": {"warm": False},
    "protocol_version": 2,
    "trained_at": "2026-09-05T00:52:09.283983",
}


def _canonical_deck_counts(counts: dict[str, int]) -> str:
    """Wrapper asserting the module hash matches the documented normalization."""
    return _canonical_deck_hash(_normalize_deck_counts(counts))


class TestManifestParsing:
    def test_v2_manifest_roundtrip(self) -> None:
        spec = ModelSpec.from_manifest(_UWTEMPO_MANIFEST_V2)
        assert spec.model_id == "UWTempo/ver2"
        assert spec.deck_similarity_threshold == 0.60
        assert spec.promotion_win_rate_threshold == 0.50
        assert spec.checkpoint_hash == _REAL_CHECKPOINT_HASH
        assert spec.promotion_status == "uncertified"
        assert spec.capabilities["warm"] is False
        assert spec.deck_counts["island"] == 7
        assert "plains" not in spec.deck_counts  # real deck has no Plains
        assert spec.deck_size == 60

    def test_manifest_matches_real_training_deck(self) -> None:
        """The fixture deck is the real xmage/decks/UWTempo.dck mainboard."""
        counts = _UWTEMPO_MANIFEST_V2["deck"]["deck_counts"]
        assert sum(counts.values()) == 60 and len(counts) == 17
        spec = ModelSpec.from_manifest(_UWTEMPO_MANIFEST_V2)
        assert spec.deck_hash == _REAL_DECK_HASH

    def test_legacy_v1_manifest_rejected_explicitly(self) -> None:
        v1 = dict(_UWTEMPO_MANIFEST_V2)
        v1.pop("schema_version")
        with pytest.raises(ManifestError, match="schema_version"):
            ModelSpec.from_manifest(v1)

    def test_blank_model_id_rejected(self) -> None:
        bad = deepcopy(_UWTEMPO_MANIFEST_V2)
        bad["model_id"] = "   "
        with pytest.raises(ManifestError, match="model_id"):
            ModelSpec.from_manifest(bad)

    def test_missing_versions_rejected(self) -> None:
        for key in ("encoder_version", "action_schema_version"):
            bad = deepcopy(_UWTEMPO_MANIFEST_V2)
            bad.pop(key)
            with pytest.raises(ManifestError, match=key):
                ModelSpec.from_manifest(bad)
        bad = deepcopy(_UWTEMPO_MANIFEST_V2)
        bad.pop("value_target")
        with pytest.raises(ManifestError, match="value_target"):
            ModelSpec.from_manifest(bad)

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
        ok = deepcopy(_UWTEMPO_MANIFEST_V2)
        ok["promotion_status"] = "certified"
        ok["certification"] = _cert_full()
        spec = ModelSpec.from_manifest(ok)
        assert spec.promotion_status == "certified"
        assert spec.certification["checkpoint_hash"] == _REAL_CHECKPOINT_HASH

    def test_uncertified_may_omit_certification(self) -> None:
        spec = ModelSpec.from_manifest(_UWTEMPO_MANIFEST_V2)
        assert spec.promotion_status == "uncertified"

    def test_rejected_status_may_not_carry_certification(self) -> None:
        bad = deepcopy(_UWTEMPO_MANIFEST_V2)
        bad["promotion_status"] = "rejected"
        bad["certification"] = _cert_full()
        with pytest.raises(ManifestError, match="certification"):
            ModelSpec.from_manifest(bad)
        bad["certification"] = None
        assert ModelSpec.from_manifest(bad).promotion_status == "rejected"


class TestCertificationEvidenceNotKeyPresence:
    """Certified must be evidenced-and-bound, not merely key-shaped."""

    def _certified(self) -> dict[str, Any]:
        d = deepcopy(_UWTEMPO_MANIFEST_V2)
        d["promotion_status"] = "certified"
        d["certification"] = _cert_full()
        return d

    def test_zero_game_panel_rejected(self) -> None:
        bad = self._certified()
        bad["certification"]["panel"] = {
            "decks": ["MonoR"], "arms": [], "games": 0, "wins": 0, "losses": 0, "draws": 0,
        }
        with pytest.raises(ManifestError, match="games"):
            ModelSpec.from_manifest(bad)

    def test_counts_not_coherent_rejected(self) -> None:
        for wins, losses, draws in ((99, 40, 1), (53, 53, 4), (-1, 140, 1)):
            bad = self._certified()
            bad["certification"]["panel"]["wins"] = wins
            bad["certification"]["panel"]["losses"] = losses
            bad["certification"]["panel"]["draws"] = draws
            with pytest.raises(ManifestError):
                ModelSpec.from_manifest(bad)

    def test_missing_arm_row_is_not_complete_evidence(self) -> None:
        bad = self._certified()
        bad["certification"]["panel"]["arms"] = bad["certification"]["panel"]["arms"][:-1]
        with pytest.raises(ManifestError, match="arms"):
            ModelSpec.from_manifest(bad)

    def test_arm_games_must_sum_to_panel_games(self) -> None:
        bad = self._certified()
        bad["certification"]["panel"]["arms"][0]["games"] = 99
        with pytest.raises(ManifestError, match="sum"):
            ModelSpec.from_manifest(bad)

    def test_arm_rates_incoherent_with_wins_rejected(self) -> None:
        bad = self._certified()
        bad["certification"]["panel"]["arms"][3]["win_rate"] = 0.99
        with pytest.raises(ManifestError, match="coherent"):
            ModelSpec.from_manifest(bad)

    def test_failed_arm_returncode_is_not_evidence(self) -> None:
        bad = self._certified()
        bad["certification"]["panel"]["arms"][0]["returncode"] = 1
        with pytest.raises(ManifestError, match="returncode"):
            ModelSpec.from_manifest(bad)

    def test_certification_bound_to_other_checkpoint_rejected(self) -> None:
        bad = self._certified()
        bad["certification"]["checkpoint_hash"] = "a" * 64
        with pytest.raises(ManifestError, match="checkpoint"):
            ModelSpec.from_manifest(bad)

    def test_certification_bound_to_other_deck_rejected(self) -> None:
        bad = self._certified()
        bad["certification"]["deck_hash"] = "b" * 64
        with pytest.raises(ManifestError, match="deck"):
            ModelSpec.from_manifest(bad)

    def test_below_threshold_result_is_uncertified(self) -> None:
        bad = self._certified()
        for arm in bad["certification"]["panel"]["arms"]:
            arm["win_rate"] = 0.10
        panel = bad["certification"]["panel"]
        panel["wins"] = 100
        panel["losses"] = 900
        with pytest.raises(ManifestError, match="threshold|win rate"):
            ModelSpec.from_manifest(bad)

    def test_threshold_mismatch_rejected(self) -> None:
        bad = self._certified()
        bad["certification"]["threshold"] = 0.40
        with pytest.raises(ManifestError, match="threshold"):
            ModelSpec.from_manifest(bad)

    def test_evidence_with_null_timestamp_rejected(self) -> None:
        bad = self._certified()
        bad["certification"]["evaluated_at"] = None
        with pytest.raises(ManifestError, match="evaluated_at"):
            ModelSpec.from_manifest(bad)

    def test_dup_opponents_in_panel_rejected(self) -> None:
        bad = self._certified()
        bad["certification"]["panel"]["decks"][1] = bad["certification"]["panel"]["decks"][0]
        with pytest.raises(ManifestError, match="duplicate|arms"):
            ModelSpec.from_manifest(bad)


class TestValueTargetSemantics:
    def test_trainer_value_semantics_accepted(self) -> None:
        spec = ModelSpec.from_manifest(_UWTEMPO_MANIFEST_V2)
        assert spec.value_target["kind"] == "game_result_label"
        assert spec.value_target["perspective"] == "recording_player"

    def test_invented_search_blend_rejected(self) -> None:
        bad = deepcopy(_UWTEMPO_MANIFEST_V2)
        bad["value_target"] = {
            "kind": "search_blended",
            "perspective": "actor",
            "range": [-1.0, 1.0],
            "terminal_handling": "win=1 loss=-1 draw=0",
            "truncation_handling": "bootstrapped_at_cap",
            "search_blend": {"strength": 0.35, "source": "mcts_visits"},
        }
        with pytest.raises(ManifestError, match="search_blend|search bl"):
            ModelSpec.from_manifest(bad)

    def test_unknown_kind_rejected(self) -> None:
        bad = deepcopy(_UWTEMPO_MANIFEST_V2)
        bad["value_target"] = dict(
            _UWTEMPO_MANIFEST_V2["value_target"], kind="policy_prior_blend"
        )
        with pytest.raises(ManifestError, match="value_target.kind"):
            ModelSpec.from_manifest(bad)

    def test_bad_range_rejected(self) -> None:
        for rng in ((-1.0, 1.5), "[-1,1]", [-1, float("nan")], [1.0, -1.0]):
            bad = deepcopy(_UWTEMPO_MANIFEST_V2)
            bad["value_target"] = dict(_UWTEMPO_MANIFEST_V2["value_target"], range=rng)
            with pytest.raises(ManifestError, match="range"):
                ModelSpec.from_manifest(bad)


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

    @pytest.mark.parametrize("what", ["deck_similarity_threshold", "promotion_win_rate_threshold"])
    def test_non_finite_or_out_of_range_thresholds_rejected(self, what: str) -> None:
        for value in (float("nan"), float("inf"), -0.1, 1.1, "0.6", True, None):
            bad = deepcopy(_UWTEMPO_MANIFEST_V2)
            bad["gate"][what] = value
            with pytest.raises(ManifestError, match=what.replace("_", "_")):
                ModelSpec.from_manifest(bad)


class TestDeckRepresentation:
    @pytest.mark.parametrize("case", ["negative_counts", "zero_counts", "bool_counts", "float_counts"])
    def test_invalid_counts_rejected(self, case: str) -> None:
        bad = deepcopy(_UWTEMPO_MANIFEST_V2)
        counts = bad["deck"]["deck_counts"]
        if case == "negative_counts":
            counts["Island"] = -7
        elif case == "zero_counts":
            counts["Island"] = 0
        elif case == "bool_counts":
            counts["Island"] = True
        else:
            counts["Island"] = 2.5
        with pytest.raises(ManifestError, match="deck"):
            ModelSpec.from_manifest(bad)

    def test_blank_card_name_rejected(self) -> None:
        bad = deepcopy(_UWTEMPO_MANIFEST_V2)
        bad["deck"]["deck_counts"]["   "] = 3
        with pytest.raises(ManifestError, match="blank"):
            ModelSpec.from_manifest(bad)

    def test_size_must_equal_deck_counts_sum(self) -> None:
        bad = deepcopy(_UWTEMPO_MANIFEST_V2)
        bad["deck"]["size"] = 999
        with pytest.raises(ManifestError, match="size"):
            ModelSpec.from_manifest(bad)

    def test_normalized_name_collision_aggregates(self) -> None:
        counts = _normalize_deck_counts({"Island": 5, "island": 2})
        assert counts == {"island": 7}

    def test_mixed_case_sources_hash_identically(self) -> None:
        a = _normalize_deck_counts({"Island": 7, "Plains": 4})
        b = _normalize_deck_counts({"island": 7, " plains ": 4})
        assert a == b
        assert _canonical_deck_hash(a) == _canonical_deck_hash(b)

    def test_zero_counts_are_rejected_not_dropped(self) -> None:
        with pytest.raises(ManifestError):
            _normalize_deck_counts({"Island": 0, "Plains": 4})

    def test_hash_is_order_and_whitespace_stable(self) -> None:
        a = _canonical_deck_counts({"Island": 7, "Plains": 4})
        b = _canonical_deck_counts({"Plains": 4, "Island": 7})
        c = _canonical_deck_counts({"Island ": 7, "Plains": 4})
        assert a == b == c

    def test_hash_sensitive_to_count_changes(self) -> None:
        assert _canonical_deck_counts({"Island": 7}) != _canonical_deck_counts({"Island": 6})

    def test_manifest_deck_hash_mismatch_rejected(self) -> None:
        bad = deepcopy(_UWTEMPO_MANIFEST_V2)
        bad["deck"]["deck_hash"] = "0" * 64
        with pytest.raises(ManifestError, match="deck_hash"):
            ModelSpec.from_manifest(bad)

    def test_missing_or_nonhex_deck_hash_rejected(self) -> None:
        for deck_hash in (None, "", "z" * 64, "53280F86D4EF71EF7AE304F6ED4F0ECA2B8B00CB5242AC6D8F781142D868D907"):
            bad = deepcopy(_UWTEMPO_MANIFEST_V2)
            bad["deck"]["deck_hash"] = deck_hash
            with pytest.raises(ManifestError, match="deck_hash"):
                ModelSpec.from_manifest(bad)


class TestIdentity:
    def test_checkpoint_hash_is_immutable_identity_seed(self) -> None:
        spec = ModelSpec.from_manifest(_UWTEMPO_MANIFEST_V2)
        assert spec.checkpoint_hash != spec.model_id
        assert len(spec.checkpoint_hash) == 64

    @pytest.mark.parametrize("bad_hash", ["z" * 64, "A" * 64, "cea84411", 12345, None])
    def test_nonhex_or_wrong_length_checkpoint_hash_rejected(self, bad_hash: Any) -> None:
        bad = deepcopy(_UWTEMPO_MANIFEST_V2)
        bad["checkpoint_hash"] = bad_hash
        with pytest.raises(ManifestError, match="checkpoint_hash"):
            ModelSpec.from_manifest(bad)

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
