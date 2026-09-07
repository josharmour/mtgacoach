"""Multi-Model Model Zoo Client for MTGA Coach.

Implements the Model Zoo manifest contract, selecting the best trained neural model
for the hero's deck and format, firing non-blocking warm requests during mulligans,
and providing seamless fallback to heuristic lookahead when off-distribution.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from arenamcp.format_profile import FormatProfile
from arenamcp.magezero_gating import compute_count_weighted_jaccard

logger = logging.getLogger(__name__)


class ManifestError(ValueError):
    """A model manifest failed v2 contract validation (explicit, not silent)."""


def _canonical_deck_hash(counts: dict[str, int]) -> str:
    """Canonical sha256 over normalized, sorted card entries (ws04 contract).

    NFC + casefold + whitespace collapse; counts clamped >= 0; zero/negative
    entries and blank names dropped; list order canonical. Excludes sideboard
    structurally (deck dict has no sideboard field merged here).
    """
    import hashlib
    import unicodedata

    normalized: list[list[Any]] = []
    for name, count in (counts or {}).items():
        n = unicodedata.normalize("NFC", str(name)).casefold()
        n = " ".join(n.split())
        try:
            c = max(0, int(count))
        except (TypeError, ValueError):
            continue
        if c > 0 and n:
            normalized.append([n, c])
    normalized.sort()
    payload = json.dumps({"cards": normalized}, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_CERTIFICATION_REQUIRED_KEYS = (
    "evaluated_at",
    "criteria_version",
    "panel",
    "aggregation",
    "threshold",
)


def _validate_certification(cert: Any) -> None:
    """Certification evidence must be complete for status == certified."""
    if not isinstance(cert, dict):
        raise ManifestError(
            "certification: status 'certified' requires a complete certification "
            "evidence block; missing or historical unverifiable evidence means "
            "uncertified"
        )
    missing = [k for k in _CERTIFICATION_REQUIRED_KEYS if k not in cert]
    if missing:
        raise ManifestError(f"certification: missing required keys {missing}")
    panel = cert["panel"]
    if not isinstance(panel, dict) or "games" not in panel or "wins" not in panel:
        raise ManifestError("certification: panel must record games/wins/losses/draws")


@dataclass(frozen=True)
class ModelSpec:
    """Specification of a trained neural model parsed from its manifest.json."""

    model_id: str
    deck: str
    version: int
    format_family: str
    deck_size: int
    singleton: bool
    commander: str | None = None
    deck_counts: dict[str, int] = field(default_factory=dict)
    gate_threshold: float = 0.60
    gauntlet: tuple[str, ...] = field(default_factory=tuple)
    gauntlet_win_rate: float = 0.0
    trained_at: str = ""
    is_resident: bool = False
    is_warming: bool = False
    # ---- v2 contract fields (task 04) ----
    schema_version: int = 1
    deck_similarity_threshold: float = 0.60
    promotion_win_rate_threshold: float = 0.50
    checkpoint_hash: str = ""
    deck_hash: str = ""
    encoder_version: str = ""
    action_schema_version: str = ""
    value_target: dict[str, Any] = field(default_factory=dict)
    promotion_status: str = "uncertified"
    certification: dict[str, Any] | None = None
    capabilities: dict[str, Any] = field(default_factory=lambda: {"warm": False})
    protocol_version: int = 1

    @property
    def label(self) -> str:
        return f"MageZero {self.deck} v{self.version}"

    @classmethod
    def from_dict(cls, data: dict[str, Any], is_resident: bool = False) -> ModelSpec:
        fmt = data.get("format") or {}
        return cls(
            model_id=str(data.get("model_id") or f"{data.get('deck')}/ver{data.get('version', 1)}"),
            deck=str(data.get("deck") or "Unknown"),
            version=int(data.get("version") or 1),
            format_family=str(fmt.get("family") or "constructed"),
            deck_size=int(fmt.get("deck_size") or 60),
            singleton=bool(fmt.get("singleton", False)),
            commander=data.get("commander"),
            deck_counts=dict(data.get("deck_counts") or {}),
            gate_threshold=float(data.get("gate_threshold") or 0.60),
            gauntlet=tuple(data.get("gauntlet") or ()),
            gauntlet_win_rate=float(data.get("gauntlet_win_rate") or 0.0),
            trained_at=str(data.get("trained_at") or ""),
            is_resident=is_resident,
        )

    @classmethod
    def from_manifest(cls, data: dict[str, Any], is_resident: bool = False) -> ModelSpec:
        """Strict v2 manifest parser (task 04). Rejects silently-fabricated fields.

        v1 manifests are rejected with ManifestError (missing schema_version):
        the old format carried the overloaded gate_threshold and no checkpoint
        identity, so accepting it would re-introduce silent-certification bugs.
        """
        if not isinstance(data, dict):
            raise ManifestError("manifest must be an object")
        if "schema_version" not in data:
            raise ManifestError(
                "manifest missing schema_version (v2 contract required; refusing "
                "to guess from a v1 manifest)"
            )
        if int(data.get("schema_version") or 0) != 2:
            raise ManifestError(
                f"unsupported manifest schema_version {data.get('schema_version')} (only 2)"
            )

        deck_block = data.get("deck") or {}
        if not isinstance(deck_block, dict) or not deck_block.get("deck_counts"):
            raise ManifestError("deck: deck_counts required (no invented defaults)")
        counts = {str(k): int(v) for k, v in deck_block.get("deck_counts", {}).items()}
        fmt = data.get("format") or {}
        gate = data.get("gate") or {}
        if not isinstance(gate, dict) or "deck_similarity_threshold" not in gate or \
                "promotion_win_rate_threshold" not in gate:
            raise ManifestError(
                "gate: separate deck_similarity_threshold and "
                "promotion_win_rate_threshold required (overloaded "
                "gate_threshold is not accepted in v2)"
            )
        if "gate_threshold" in data:
            raise ManifestError(
                "gate_threshold is not a v2 field; use "
                "deck_similarity_threshold / promotion_win_rate_threshold"
            )
        status = str(data.get("promotion_status") or "uncertified")
        if status not in ("certified", "uncertified", "rejected"):
            raise ManifestError(f"promotion_status must be certified|uncertified|rejected, got {status!r}")
        cert = data.get("certification")
        if status == "certified":
            _validate_certification(cert)

        checkpoint_hash = str(data.get("checkpoint_hash") or "")
        if len(checkpoint_hash) != 64:
            raise ManifestError(
                "checkpoint_hash: 64-hex sha256 of the immutable checkpoint bytes "
                "required; a human-readable model name is not model identity"
            )
        deck_hash = str(deck_block.get("deck_hash") or "")
        expected_hash = _canonical_deck_hash(counts)
        if deck_hash and deck_hash != expected_hash:
            raise ManifestError(
                f"deck_hash mismatch: manifest {deck_hash} != canonical {expected_hash}"
            )

        format_family = str(fmt.get("family") or "constructed")
        return cls(
            model_id=str(data.get("model_id") or ""),
            deck=str(deck_block.get("name") or data.get("deck") or "Unknown"),
            version=int(data.get("version") or 1),
            format_family=format_family,
            deck_size=int(deck_block.get("size") or sum(counts.values())),
            singleton=bool(deck_block.get("singleton", False)),
            commander=deck_block.get("commander"),
            deck_counts=counts,
            gate_threshold=float(gate["deck_similarity_threshold"]),
            schema_version=2,
            deck_similarity_threshold=float(gate["deck_similarity_threshold"]),
            promotion_win_rate_threshold=float(gate["promotion_win_rate_threshold"]),
            checkpoint_hash=checkpoint_hash,
            deck_hash=deck_hash or expected_hash,
            encoder_version=str(data.get("encoder_version") or ""),
            action_schema_version=str(data.get("action_schema_version") or ""),
            value_target=dict(data.get("value_target") or {}),
            promotion_status=status,
            certification=dict(cert) if isinstance(cert, dict) else None,
            capabilities=dict(data.get("capabilities") or {"warm": False}),
            protocol_version=int(data.get("protocol_version") or 2),
            trained_at=str(data.get("trained_at") or ""),
            is_resident=is_resident,
        )


@dataclass(frozen=True)
class ModelSelection:
    """Result of model zoo selection for a given match."""

    model_spec: ModelSpec
    similarity: float
    label: str
    is_resident: bool


# Canonical default manifest for UWTempo/ver2 (bundled fallback)
_DEFAULT_UWTEMPO_MANIFEST = {
    "manifest_version": 1,
    "model_id": "UWTempo/ver2",
    "deck": "UWTempo",
    "version": 2,
    "format": {"family": "constructed", "variant": "standard", "deck_size": 60, "singleton": False},
    "commander": None,
    "gate_threshold": 0.60,
    "gauntlet_win_rate": 0.26,
    "deck_counts": {
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
        "Island": 7,
        "Plains": 4,
        "Seachrome Coast": 4,
        "Adarkar Wastes": 4,
        "Deserted Beach": 3,
        "Eiganjo, Seat of the Empire": 1,
        "Otawara, Soaring City": 1,
    },
}


class ModelZooClient:
    """Manages model discovery, selection, and non-blocking warming."""

    _models: list[ModelSpec] = [ModelSpec.from_dict(_DEFAULT_UWTEMPO_MANIFEST, is_resident=True)]
    _last_refresh: float = 0.0
    _resident_models: set[str] = {"UWTempo/ver2"}
    _lock = threading.Lock()

    @classmethod
    def refresh(cls, candidate_urls: list[str] | None = None) -> list[ModelSpec]:
        """Fetch active model manifests from inference servers (cached 60s)."""
        now = time.time()
        if now - cls._last_refresh < 60.0 and cls._models:
            return list(cls._models)

        from arenamcp.magezero_client import MageZeroClient

        base_url = MageZeroClient.get_active_endpoint()
        if not base_url:
            return list(cls._models)

        import urllib.request
        try:
            req = urllib.request.Request(f"{base_url}/models", headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models_list = data.get("models") or []
                resident_set = set(data.get("resident") or [])
                parsed = [
                    ModelSpec.from_dict(m, is_resident=(m.get("model_id") in resident_set))
                    for m in models_list
                ]
                with cls._lock:
                    if parsed:
                        cls._models = parsed
                        cls._resident_models = resident_set
                    cls._last_refresh = now
        except Exception as e:
            logger.debug("Failed to query /models from inference server: %s", e)

        return list(cls._models)

    @classmethod
    def select(
        cls, profile: FormatProfile, hero_deck: Counter[str] | list[str]
    ) -> ModelSelection | None:
        """Select the highest-quality resident or warming model matching the hero deck."""
        hero_counts = Counter(hero_deck)
        if not hero_counts:
            return None

        candidates: list[tuple[float, ModelSpec]] = []
        for spec in cls._models:
            # 1. Format family and deck size must match
            if spec.format_family != profile.family:
                continue
            if profile.deck_size and spec.deck_size != profile.deck_size:
                continue

            # 2. For Brawl / Commander, commander name must match
            if spec.commander is not None:
                if not profile.commander_names or spec.commander not in profile.commander_names:
                    continue

            # 3. Compute match score against model's reference deck
            spec_counter = Counter(spec.deck_counts)
            if not spec_counter:
                continue

            total_seen = sum(hero_counts.values())
            if total_seen < 40:
                # In-match revealed cards: precision against reference deck
                matching_cards = sum(
                    min(count, spec_counter.get(c, 0)) for c, count in hero_counts.items()
                )
                score = matching_cards / total_seen
                distinct_seen = len([c for c in hero_counts if c in spec_counter])
                if score >= 0.75 and distinct_seen >= 2:
                    candidates.append((score, spec))
            else:
                # Full decklist available: count-weighted Jaccard
                sim = compute_count_weighted_jaccard(hero_counts, spec_counter)
                if sim >= spec.gate_threshold:
                    candidates.append((sim, spec))

        if not candidates:
            return None

        # Sort candidates by:
        # 1. is_resident (prioritize currently loaded GPU models)
        # 2. similarity score descending
        # 3. gauntlet win rate descending
        candidates.sort(
            key=lambda item: (
                1 if item[1].is_resident else 0,
                item[0],
                item[1].gauntlet_win_rate,
            ),
            reverse=True,
        )

        best_sim, best_spec = candidates[0]
        return ModelSelection(
            model_spec=best_spec,
            similarity=round(best_sim, 3),
            label=best_spec.label,
            is_resident=best_spec.is_resident,
        )

    @classmethod
    def warm(cls, model_id: str) -> None:
        """Send asynchronous non-blocking warm request during mulligans."""
        from arenamcp.magezero_client import MageZeroClient

        base_url = MageZeroClient.get_active_endpoint()
        if not base_url:
            return

        def _do_warm():
            try:
                import urllib.request

                req = urllib.request.Request(
                    f"{base_url}/models/{model_id}/warm",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    logger.info("Warmed model %s on inference server", model_id)
            except Exception as e:
                logger.debug("Failed to warm model %s: %s", model_id, e)

        thread = threading.Thread(target=_do_warm, daemon=True, name=f"warm-{model_id}")
        thread.start()
