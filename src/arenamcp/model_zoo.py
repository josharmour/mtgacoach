"""Multi-Model Model Zoo Client for MTGA Coach.

Implements the Model Zoo manifest contract, selecting the best trained neural model
for the hero's deck and format, firing non-blocking warm requests during mulligans,
and providing seamless fallback to heuristic lookahead when off-distribution.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from arenamcp.format_profile import FormatProfile
from arenamcp.magezero_gating import compute_count_weighted_jaccard

logger = logging.getLogger(__name__)


class ManifestError(ValueError):
    """A model manifest failed v2 contract validation (explicit, not silent)."""


_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _normalize_deck_counts(counts: Any) -> dict[str, int]:
    """Validated deck representation: the single source for deck size AND hash.

    Every entry must be a positive integer count for a non-blank name. Names
    are NFC-normalized, casefolded and whitespace-collapsed; collisions after
    normalization are aggregated by summation (a raw "Island"/"island" mix and
    "Island" denote the same card). Absent cards are simply omitted — zero or
    negative counts are rejected rather than silently clamped or dropped, and
    fractional/bool/non-numeric counts are rejected outright. Sideboard data
    is structurally excluded (the v2 deck block carries mainboard counts only).
    """
    if not isinstance(counts, dict) or not counts:
        raise ManifestError("deck: deck_counts required (no invented defaults)")
    normalized: dict[str, int] = {}
    for name, count in counts.items():
        n = unicodedata.normalize("NFC", str(name)).casefold()
        n = " ".join(n.split())
        if not n:
            raise ManifestError(f"deck: blank card name in deck_counts: {name!r}")
        if isinstance(count, bool) or not isinstance(count, (int, float)) or (
                isinstance(count, float) and not float(count).is_integer()):
            raise ManifestError(
                f"deck: card count for {name!r} must be an integer, got {count!r}"
            )
        c = int(count)
        if c <= 0:
            raise ManifestError(
                f"deck: card count for {name!r} must be a positive integer, got "
                f"{count!r} (omit absent cards; zero/negative counts are rejected, "
                "not silently clamped)"
            )
        normalized[n] = normalized.get(n, 0) + c
    return normalized


def _canonical_deck_hash(normalized_counts: dict[str, int]) -> str:
    """Canonical sha256 over the VALIDATED deck representation.

    Input must come from _normalize_deck_counts (same validated representation
    the parser records); this function performs no permissive normalization of
    its own. Entries are emitted as a sorted JSON list of [name, count] pairs.
    """
    import hashlib

    normalized = sorted([name, int(count)] for name, count in normalized_counts.items())
    payload = json.dumps({"cards": normalized}, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_probability(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
        raise ManifestError(
            f"{what}: must be a finite number in [0.0, 1.0], got {value!r}"
        )
    return float(value)


def _require_nonempty_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{what}: non-empty string required, got {value!r}")
    return value


# ---- value-target semantics -------------------------------------------------
# Evidence (read-only, /mnt/repos/magezero @6b54f51):
#   dataset.py:25  row = [policy(A), resultLabel, stateScore, isPlayer, actionType]
#   dataset.py:136 value_t = clamp(resultLabel, -1.0, 1.0)
#   train.py:271   lv = mse(value_pred, batch_value_labels)
# i.e. the value target is the per-state game-result label from the recording
# player's perspective. There is NO search blending and NO truncation
# bootstrapping in the trainer; manifests claiming such semantics are
# unverified and rejected rather than certified by name alone.
_VALUE_TARGET_KINDS = ("game_result_label",)
_VALUE_TARGET_PERSPECTIVES = ("recording_player",)


def _validate_value_target(vt: Any) -> dict[str, Any]:
    if not isinstance(vt, dict):
        raise ManifestError("value_target: required with trainer-evidenced semantics")
    for key in ("kind", "perspective", "range", "terminal_handling"):
        if key not in vt:
            raise ManifestError(f"value_target: missing required key {key!r}")
    kind = _require_nonempty_str(vt["kind"], "value_target.kind")
    if kind not in _VALUE_TARGET_KINDS:
        raise ManifestError(
            f"value_target.kind {kind!r} is not a semantics verified in the magezero "
            f"trainer (dataset.py resultLabel only); verified kinds: "
            f"{list(_VALUE_TARGET_KINDS)}"
        )
    perspective = _require_nonempty_str(vt["perspective"], "value_target.perspective")
    if perspective not in _VALUE_TARGET_PERSPECTIVES:
        raise ManifestError(
            f"value_target.perspective {perspective!r} unverified; "
            f"trainer label is the recording player's (isPlayer-signed) resultLabel"
        )
    rng = vt["range"]
    if (not isinstance(rng, (list, tuple)) or len(rng) != 2
            or any(isinstance(x, bool) or not isinstance(x, (int, float))
                   or not math.isfinite(float(x)) for x in rng)
            or not float(rng[0]) < float(rng[1])):
        raise ManifestError(f"value_target.range: finite [lo, hi] with lo < hi required, got {rng!r}")
    # The trainer clamps the result label to exactly [-1.0, 1.0]
    # (dataset.py:136 torch.clamp(..., -1.0, 1.0)); any other advertised range
    # does not match the evidenced training semantics.
    if abs(float(rng[0]) - (-1.0)) > 1e-9 or abs(float(rng[1]) - 1.0) > 1e-9:
        raise ManifestError(
            f"value_target.range must be exactly [-1.0, 1.0] (trainer clamp), got {rng!r}"
        )
    _require_nonempty_str(vt["terminal_handling"], "value_target.terminal_handling")
    if "search_blend" in vt:
        raise ManifestError(
            "value_target.search_blend present but no search blend exists in the "
            "magezero trainer; do not certify invented training semantics"
        )
    trunc = vt.get("truncation_handling")
    if trunc is not None and _require_nonempty_str(
            trunc, "value_target.truncation_handling") not in ("none", "unknown"):
        raise ManifestError(
            "value_target.truncation_handling: only 'none'/'unknown' are supported; "
            "the trainer has no truncation bootstrap to certify"
        )
    return dict(vt)


# ---- certification evidence -------------------------------------------------
# Certified status must be EVIDENCED, not key-shaped. Requirements
# (runner evidence: runner.py acceptance-gate + candidate_eval record):
#   - bound to the manifest's checkpoint/deck identity (immutable hashes)
#   - evaluated_at / criteria_version present and real
#   - a complete opponent panel: per-arm records with finite win rates on
#     successful arm runs, coherent finite counts, and the aggregate mean
#     win rate meeting the promotion threshold actually gated against
_CERTIFICATION_REQUIRED_KEYS = (
    "evaluated_at",
    "criteria_version",
    "checkpoint_hash",
    "deck_hash",
    "panel",
    "aggregation",
    "threshold",
)


def _validate_certification(
    cert: Any, checkpoint_hash: str, deck_hash: str, promotion_threshold: float
) -> dict[str, Any]:
    """Certification evidence must be complete, coherent and identity-bound."""
    if not isinstance(cert, dict):
        raise ManifestError(
            "certification: status 'certified' requires a complete certification "
            "evidence block; missing/historical unverifiable evidence means "
            "uncertified, never certified-by-default"
        )
    missing = [k for k in _CERTIFICATION_REQUIRED_KEYS if k not in cert]
    if missing:
        raise ManifestError(f"certification: missing required keys {missing}")

    # Identity binding: the certification claim must name the exact model.
    for key, expected, label in (
        ("checkpoint_hash", checkpoint_hash, "checkpoint"),
        ("deck_hash", deck_hash, "deck"),
    ):
        got = str(cert[key] or "")
        if got != expected:
            raise ManifestError(
                f"certification: {key} not bound to this manifest {label} "
                f"(certification {got!r} != manifest {expected!r})"
            )

    evaluated_at = _require_nonempty_str(cert["evaluated_at"], "certification.evaluated_at")
    try:
        datetime.fromisoformat(evaluated_at)
    except ValueError as e:
        raise ManifestError(
            f"certification.evaluated_at: ISO-8601 timestamp required, got {evaluated_at!r}"
        ) from e
    _require_nonempty_str(cert["criteria_version"], "certification.criteria_version")
    _require_nonempty_str(cert["aggregation"], "certification.aggregation")
    cert_threshold = _require_probability(
        cert["threshold"], "certification.threshold"
    )
    if abs(cert_threshold - promotion_threshold) > 1e-9:
        raise ManifestError(
            "certification: threshold "
            f"{cert_threshold} does not match manifest promotion_win_rate_threshold "
            f"{promotion_threshold}"
        )

    panel = cert["panel"]
    if not isinstance(panel, dict):
        raise ManifestError("certification: panel must be a record of the opponent panel")
    games = panel.get("games")
    wins = panel.get("wins")
    losses = panel.get("losses")
    draws = panel.get("draws")
    for label, v in (("games", games), ("wins", wins), ("losses", losses), ("draws", draws)):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ManifestError(
                f"certification: panel.{label} must be a non-negative integer, got {v!r}"
            )
    if games <= 0:
        raise ManifestError(
            "certification: panel.games must be a positive game count; a certified "
            "panel with zero evaluated games is not certification"
        )
    if wins + losses + draws != games:
        raise ManifestError(
            f"certification: panel wins+losses+draws ({wins + losses + draws}) "
            f"must equal games ({games})"
        )
    decks = panel.get("decks")
    if not isinstance(decks, list) or not decks or not all(
            isinstance(d, str) and d.strip() for d in decks):
        raise ManifestError(
            "certification: panel.decks must list the complete opponent panel"
        )
    if isinstance(decks, list) and len(set(decks)) != len(decks):
        raise ManifestError("certification: panel.decks contains duplicate opponents")

    # Per-arm records (runner-shaped complete panel).
    arms = panel.get("arms")
    if not isinstance(arms, list) or not arms:
        raise ManifestError(
            "certification: panel.arms must record every opponent arm of the "
            "promotion evaluation (incomplete panels are uncertified)"
        )
    arm_games_total = 0
    arm_wr_weighted = 0.0
    arm_opponents: list[str] = []
    for arm in arms:
        if not isinstance(arm, dict):
            raise ManifestError("certification: each panel.arm must be a record")
        opponent = _require_nonempty_str(
            arm.get("opponent"), "certification: panel.arm.opponent"
        )
        if opponent in arm_opponents:
            raise ManifestError(
                f"certification: duplicate panel arm for opponent {opponent!r}"
            )
        arm_opponents.append(opponent)
        games_i = arm.get("games")
        if isinstance(games_i, bool) or not isinstance(games_i, int) or games_i < 1:
            raise ManifestError(
                f"certification: arm {opponent!r} games must be a positive integer, got {games_i!r}"
            )
        win_rate = arm.get("win_rate")
        if isinstance(win_rate, bool) or not isinstance(win_rate, (int, float)) \
                or not math.isfinite(float(win_rate)) or not 0.0 <= float(win_rate) <= 1.0:
            raise ManifestError(
                f"certification: arm {opponent!r} win_rate must be finite in [0,1], "
                f"got {win_rate!r}"
            )
        returncode = arm.get("returncode")
        if returncode != 0:
            raise ManifestError(
                f"certification: arm {opponent!r} returncode {returncode!r} != 0; "
                "failed arm runs are not certification evidence"
            )
        arm_games_total += games_i
        arm_wr_weighted += float(win_rate) * games_i
    if set(arm_opponents) != set(decks):
        raise ManifestError(
            f"certification: panel.arms opponents {sorted(arm_opponents)} must equal "
            f"panel.decks {sorted(decks)}"
        )
    if arm_games_total != games:
        raise ManifestError(
            f"certification: arm games sum ({arm_games_total}) must equal panel games ({games})"
        )
    mean_wr = arm_wr_weighted / games
    if abs(mean_wr - wins / games) > 1e-6:
        raise ManifestError(
            "certification: arm-weighted mean win rate "
            f"{mean_wr:.6f} is incoherent with panel wins/games ({wins}/{games})"
        )
    if wins / games < promotion_threshold:
        raise ManifestError(
            f"certification: panel win rate {wins}/{games} does not meet the "
            f"promotion threshold {promotion_threshold}; a below-threshold result "
            "is uncertified regardless of keys present"
        )
    return dict(cert)


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

        model_id = _require_nonempty_str(data.get("model_id"), "model_id")

        block = data.get("deck") or {}
        if not isinstance(block, dict) or not block.get("deck_counts"):
            raise ManifestError("deck: deck_counts required (no invented defaults)")
        # Single validated representation drives size AND hash: no silently
        # dropped entries, no raw/card-name ambiguity, collisions aggregated.
        counts = _normalize_deck_counts(block.get("deck_counts"))
        deck_size_declared = block.get("size")
        if deck_size_declared is not None:
            if isinstance(deck_size_declared, bool) or not isinstance(deck_size_declared, int) \
                    or deck_size_declared <= 0:
                raise ManifestError(
                    f"deck.size: positive integer required, got {deck_size_declared!r}"
                )
            actual_size = sum(counts.values())
            if deck_size_declared != actual_size:
                raise ManifestError(
                    f"deck.size {deck_size_declared} != sum of deck_counts "
                    f"({actual_size}); the declared size must match the validated deck"
                )

        fmt = data.get("format") or {}
        if not isinstance(fmt, dict):
            raise ManifestError("format: object required")
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
        deck_similarity_threshold = _require_probability(
            gate["deck_similarity_threshold"], "gate.deck_similarity_threshold"
        )
        promotion_win_rate_threshold = _require_probability(
            gate["promotion_win_rate_threshold"], "gate.promotion_win_rate_threshold"
        )

        encoder_version = _require_nonempty_str(data.get("encoder_version"), "encoder_version")
        action_schema_version = _require_nonempty_str(
            data.get("action_schema_version"), "action_schema_version"
        )
        value_target = _validate_value_target(data.get("value_target"))

        status = str(data.get("promotion_status") or "uncertified")
        if status not in ("certified", "uncertified", "rejected"):
            raise ManifestError(f"promotion_status must be certified|uncertified|rejected, got {status!r}")

        checkpoint_hash = str(data.get("checkpoint_hash") or "")
        if not _SHA256_HEX_RE.fullmatch(checkpoint_hash):
            raise ManifestError(
                "checkpoint_hash: 64-hex lowercase sha256 of the immutable "
                "checkpoint bytes required; synthetic/human names are not identity"
            )
        deck_hash = str(block.get("deck_hash") or "")
        expected_hash = _canonical_deck_hash(counts)
        if not _SHA256_HEX_RE.fullmatch(deck_hash):
            raise ManifestError(
                f"deck_hash: 64-hex sha256 required (canonical {expected_hash})"
            )
        if deck_hash != expected_hash:
            raise ManifestError(
                f"deck_hash mismatch: manifest {deck_hash} != canonical {expected_hash}"
            )

        cert = data.get("certification")
        if status == "certified":
            _validate_certification(
                cert, checkpoint_hash, deck_hash, promotion_win_rate_threshold
            )
        elif status != "uncertified" and cert is not None:
            raise ManifestError("certification: must be null unless promotion_status is 'certified'")

        format_family = str(fmt.get("family") or "constructed")
        return cls(
            model_id=model_id,
            deck=str(block.get("name") or data.get("deck") or "Unknown"),
            version=int(data.get("version") or 1),
            format_family=format_family,
            deck_size=sum(counts.values()),
            singleton=bool(block.get("singleton", False)),
            commander=block.get("commander"),
            deck_counts=counts,
            gate_threshold=deck_similarity_threshold,
            schema_version=2,
            deck_similarity_threshold=deck_similarity_threshold,
            promotion_win_rate_threshold=promotion_win_rate_threshold,
            checkpoint_hash=checkpoint_hash,
            deck_hash=deck_hash,
            encoder_version=encoder_version,
            action_schema_version=action_schema_version,
            value_target=value_target,
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
