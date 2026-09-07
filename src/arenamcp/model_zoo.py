"""Multi-Model Model Zoo Client for MTGA Coach.

Implements the Model Zoo manifest contract, selecting the best trained neural model
for the hero's deck and format, firing non-blocking warm requests during mulligans,
and providing seamless fallback to heuristic lookahead when off-distribution.

Task 05 (wired discovery + honest residency):
- Model discovery is wired to the real coaching lifecycle: the same
  ``MageZeroClient`` endpoint used for /evaluate is probed for /models, with
  a bounded TTL cache and asynchronous background refresh.
- Model rows and residency are scoped to the endpoint that advertised them;
  switching endpoints invalidates the previous cache.
- Nothing is resident or certified by default: there is NO bundled fallback
  model and NO invented benchmark value. Selection only ever considers
  specs a live server reported as resident.
- Warm requests are only issued when the manifest (task 04 contract)
  explicitly declares ``capabilities.warm = True``.
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
    if cert.get("criteria_version") == "all10-v1":
        if len(decks) != 10:
            raise ManifestError(
                f"certification: criteria_version 'all10-v1' requires 10 distinct panel decks, got {len(decks)}"
            )
        if len(arms) != 10:
            raise ManifestError(
                f"certification: criteria_version 'all10-v1' requires 10 panel arms, got {len(arms)}"
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


GENERIC_BASIC_LANDS: frozenset[str] = frozenset({
    "Island",
    "Plains",
    "Swamp",
    "Mountain",
    "Forest",
    "Snow-Covered Island",
    "Snow-Covered Plains",
    "Snow-Covered Swamp",
    "Snow-Covered Mountain",
    "Snow-Covered Forest",
    "Wastes",
})


# LEGACY artifact, retained as test evidence only (task 05): the old client
# bundled this v1 manifest as a "resident" default model and implicitly carried
# the invented 0.26 gauntlet benchmark ("historical 0.26 benchmark score" from
# the task table). Production code must never re-install it: v1 manifests are
# REJECTED by ModelSpec.from_manifest, and residency can only come from a live
# server. Import target for tests that assert the bundled default is gone.
LEGACY_BUNDLED_UWTEMPO_MANIFEST_V1: dict[str, Any] = {
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
    },
}

_DEFAULT_UWTEMPO_MANIFEST = LEGACY_BUNDLED_UWTEMPO_MANIFEST_V1


class ModelZooClient:
    """Manages model discovery, selection, and non-blocking warming.

    Residency policy (task 05):

    - NOTHING is resident until a real inference server says so. The client
      starts EMPTY — no bundled fallback model, no invented resident set, no
      invented benchmark value. With no healthy endpoint, ``select()``
      returns None and the coach falls back to the heuristic path explicitly.
    - Model rows and residency are scoped to the endpoint that advertised
      them. A different active endpoint invalidates the previous cache
      before new manifests are stored: manifest content and residency are
      per-server facts, not global ones.
    - Refresh is bounded and lifecycle-friendly: first discovery is a
      synchronous bounded fetch; a stale same-endpoint cache is refreshed
      asynchronously on a daemon thread so the coaching loop never blocks.
    """

    # How long a /models snapshot is trusted before re-discovery. Bounded
    # expiry is required: residency is a server-side fact that can change.
    REFRESH_TTL_SECONDS: float = 60.0
    FETCH_TIMEOUT_SECONDS: float = 1.5

    _models_by_host: dict[str, list[ModelSpec]] = {}
    _active_host: str | None = None
    _resident_models: set[str] = set()
    _last_refresh: float = 0.0
    _last_fallback_reason: str | None = "discovery-not-run"
    _refresh_thread: threading.Thread | None = None
    _lock = threading.Lock()

    @classmethod
    def _get_auth_headers(cls) -> dict[str, str]:
        from arenamcp.magezero_client import _get_auth_headers

        return _get_auth_headers()

    @classmethod
    def reset(cls) -> None:
        """Clear all discovery state (tests and lifecycle teardown)."""
        with cls._lock:
            cls._models_by_host.clear()
            cls._active_host = None
            cls._resident_models.clear()
            cls._last_refresh = 0.0
            cls._last_fallback_reason = "discovery-reset"

    @classmethod
    def _fetch_manifests(cls, base_url: str) -> dict[str, Any]:
        """Fetch + parse /models from ``base_url``.

        Returns {"models": [ModelSpec...], "resident_ids": set[str]}.
        Residency is accepted ONLY as reported by the server; manifests are
        parsed under the strict v2 contract (task 04) — a malformed or stale
        v1 row raises instead of quietly entering selection.
        """
        import urllib.request

        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/models",
            headers={"Accept": "application/json", **cls._get_auth_headers()},
        )
        with urllib.request.urlopen(req, timeout=cls.FETCH_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                raise RuntimeError(f"/models returned HTTP {resp.status}")
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise ManifestError("/models payload must be a JSON object")

        models_list = data.get("models") or []
        if not isinstance(models_list, list):
            raise ManifestError("/models: 'models' must be a list")

        resident_raw = data.get("resident")
        if isinstance(resident_raw, dict):
            raw_ids = resident_raw.get("models") or resident_raw.get("ids") or []
        elif resident_raw is None:
            raw_ids = []
        elif isinstance(resident_raw, list):
            raw_ids = resident_raw
        else:
            raise ManifestError("/models: 'resident' must be a list or object")
        if not isinstance(raw_ids, list):
            raise ManifestError("/models: 'resident' entries malformed")

        resident_ids: set[str] = set()
        for entry in raw_ids:
            if isinstance(entry, str) and entry.strip():
                resident_ids.add(entry.strip())
            elif isinstance(entry, dict) and isinstance(entry.get("model_id"), str):
                if entry["model_id"].strip():
                    resident_ids.add(entry["model_id"].strip())
            else:
                raise ManifestError("/models: 'resident' entries malformed")

        parsed: list[ModelSpec] = []
        seen_ids: set[str] = set()
        for idx, raw_manifest in enumerate(models_list):
            if not isinstance(raw_manifest, dict):
                raise ManifestError(f"/models: entry {idx} is not an object")
            spec = ModelSpec.from_manifest(raw_manifest)
            if spec.model_id in seen_ids:
                raise ManifestError(f"/models: duplicate model_id {spec.model_id!r}")
            seen_ids.add(spec.model_id)
            spec_dict: dict[str, Any] = dict(spec.__dict__)
            spec_dict["is_resident"] = spec.model_id in resident_ids
            parsed.append(ModelSpec(**spec_dict))

        if parsed and not resident_ids:
            logger.debug(
                "ModelZoo discovery from %s advertised %d model(s) but none resident",
                base_url,
                len(parsed),
            )
        return {"models": parsed, "resident_ids": resident_ids}

    @classmethod
    def _snapshot_for(cls, host: str | None) -> list[ModelSpec]:
        """Model rows cached for ``host`` (None-returning helper for tests)."""
        if host is None:
            return []
        with cls._lock:
            return list(cls._models_by_host.get(host, []))

    @classmethod
    def refresh(
        cls,
        candidate_urls: list[str] | None = None,
        *,
        force: bool = False,
        async_ok: bool = True,
    ) -> list[ModelSpec]:
        """Bounded discovery of the served model zoo through the real client.

        Resolution order: explicit ``candidate_urls`` override, else the
        coaching client's actual endpoint (``MageZeroClient.get_active_
        endpoint()``), which itself runs the budget-bounded /healthz
        discovery with fallback reasons (task 01).

        Behavior:
        - TTL cache is scoped to the active endpoint. A cache from a
          different host is invalidated before any new data is stored.
        - First discovery is synchronous and bounded by the HTTP timeout;
          a stale cache for the SAME endpoint is refreshed asynchronously
          (daemon thread) so the coaching loop never blocks on re-discovery.
        - Empty/unavailable/malformed discovery sets an explicit fallback
          reason and leaves selection empty. This method NEVER injects a
          bundled model, never synthesizes residency, and never carries a
          resident set across endpoints.

        Returns the CURRENT model rows for the resolved endpoint (possibly
        empty — callers must treat empty as explicit fallback, not data).
        """
        from arenamcp.magezero_client import MageZeroClient

        now = time.time()
        if candidate_urls:
            base_url = candidate_urls[0].rstrip("/")
        else:
            base_url = MageZeroClient.get_active_endpoint()
            if not base_url:
                with cls._lock:
                    if cls._active_host is not None:
                        cls._models_by_host.pop(cls._active_host, None)
                    cls._active_host = None
                    cls._resident_models.clear()
                    cls._last_refresh = now
                    cls._last_fallback_reason = "no-active-coaching-endpoint"
                return []

        with cls._lock:
            cached_host = cls._active_host
            cache_fresh = (
                cached_host is not None
                and cached_host == base_url
                and (now - cls._last_refresh) < cls.REFRESH_TTL_SECONDS
                and bool(cls._models_by_host.get(cached_host, []))
            )
            if not force and cache_fresh:
                return list(cls._models_by_host.get(cached_host) or [])
            if (
                not force
                and not cache_fresh
                and async_ok
                and cached_host is not None
                and cached_host == base_url
            ):
                # Same endpoint, expired data: refresh in the background (the
                # coaching loop must not stall on re-discovery) and serve the
                # current snapshot meanwhile.
                cls._start_background_refresh_locked()
                return list(cls._models_by_host.get(cached_host, []))

        try:
            discovered = cls._fetch_manifests(base_url)
        except Exception as e:
            with cls._lock:
                cls._last_refresh = now
                if cls._active_host is not None:
                    cls._models_by_host.pop(cls._active_host, None)
                cls._models_by_host.pop(base_url, None)
                cls._active_host = None
                cls._resident_models.clear()
                cls._last_fallback_reason = f"model-discovery-failed: {type(e).__name__}"
            logger.debug("ModelZoo refresh from %s failed: %s", base_url, e)
            return []

        models: list[ModelSpec] = discovered["models"]
        resident_ids: set[str] = discovered["resident_ids"]
        with cls._lock:
            if cls._active_host is not None and cls._active_host != base_url:
                cls._models_by_host.pop(cls._active_host, None)
            cls._models_by_host[base_url] = models
            cls._active_host = base_url
            cls._resident_models = set(resident_ids)
            cls._last_refresh = now
            cls._last_fallback_reason = None if models else "model-discovery-empty"
        return list(models)

    @classmethod
    def _start_background_refresh_locked(cls) -> None:
        thread = cls._refresh_thread
        if thread is not None and thread.is_alive():
            return

        def _bg() -> None:
            try:
                cls.refresh(force=True, async_ok=False)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("ModelZoo background refresh failed: %s", e)

        cls._refresh_thread = threading.Thread(target=_bg, daemon=True, name="model-zoo-refresh")
        cls._refresh_thread.start()

    @classmethod
    def last_fallback_reason(cls) -> str | None:
        """Why the last discovery pass produced no usable model rows."""
        return cls._last_fallback_reason

    @classmethod
    def active_endpoint(cls) -> str | None:
        """The endpoint whose manifests/residency are currently cached."""
        return cls._active_host

    @classmethod
    def resident_model_ids(cls) -> set[str]:
        """Model ids the ACTIVE server reports as resident right now."""
        with cls._lock:
            return set(cls._resident_models)

    @classmethod
    def cached_models(cls) -> list[ModelSpec]:
        """Model rows cached for the active endpoint (possibly empty)."""
        host = cls._active_host
        if host is None:
            return []
        with cls._lock:
            return list(cls._models_by_host.get(host, []))

    @classmethod
    def select(
        cls,
        profile: FormatProfile,
        hero_deck: Counter[str] | list[str],
        *,
        refresh: bool = True,
    ) -> ModelSelection | None:
        """Select the best matching model among the ACTIVE server's specs.

        Only RESIDENT specs are eligible: a non-resident model is not loaded
        on the GPU, and warming is not load-bearing (task 05). With no
        discovery data, or no matching resident model, selection returns
        None — the caller preserves the heuristic path explicitly.
        """
        hero_counts = Counter(hero_deck)
        if not hero_counts:
            return None

        # Normalization must MATCH the manifest side: ModelSpec carries
        # casefolded/canonical deck_counts (task 04), so raw game-state card
        # names ("Malcolm, Alluring Scoundrel" vs "malcolm, ...") have to be
        # normalized identically before any similarity comparison, otherwise
        # a perfect deck match computes as 0.0.
        try:
            hero_counts = Counter(_normalize_deck_counts(dict(hero_counts)))
        except ManifestError:
            pass  # non-representable input (defensive); compare as-is

        if refresh:
            cls.refresh()

        from arenamcp.magezero_client import MageZeroClient
        active_ep = MageZeroClient.get_active_endpoint()
        if not active_ep or cls._active_host != active_ep:
            return None

        specs = cls.cached_models()
        candidates: list[tuple[float, ModelSpec]] = []
        for spec in specs:
            if not spec.is_resident:
                # Do not select models the server has not confirmed as loaded.
                continue
            if spec.promotion_status != "certified":
                # Uncertified and rejected models are never selected by default
                continue
            # 1. Format family and deck size must match
            if spec.format_family != profile.family:
                continue
            if profile.deck_size and spec.deck_size != profile.deck_size:
                continue

            # 2. For Brawl / Commander, commander name must match
            if spec.commander is not None:
                if not profile.commander_names or spec.commander not in profile.commander_names:
                    continue

            # 3. Compute match score against the model's reference deck
            spec_counter = Counter(spec.deck_counts)
            if not spec_counter:
                continue

            total_seen = sum(hero_counts.values())
            if total_seen < 40:
                # In-match revealed cards: precision against reference deck.
                # Two generic matching cards alone (e.g. basic lands) do NOT match.
                non_generic = [
                    c for c in hero_counts
                    if c not in GENERIC_BASIC_LANDS and c in spec_counter
                ]
                if not non_generic or total_seen < 3:
                    continue
                matching_cards = sum(
                    min(count, spec_counter.get(c, 0)) for c, count in hero_counts.items()
                )
                score = matching_cards / total_seen
                if score >= 0.75:
                    candidates.append((score, spec))
            else:
                # Full decklist available: count-weighted Jaccard against the
                # manifest's OWN similarity threshold (v2 gate block, task 04).
                sim = compute_count_weighted_jaccard(hero_counts, spec_counter)
                if sim >= spec.deck_similarity_threshold:
                    candidates.append((sim, spec))

        if not candidates:
            if not specs:
                logger.debug(
                    "ModelZoo select: no discovered models (reason: %s)",
                    cls.last_fallback_reason(),
                )
            return None

        # Rank by loading state (warming last), then similarity descending.
        # NO benchmark/win-rate tiebreak: gauntlet_win_rate is historical
        # metadata and certification is a strict, evidenced boolean — never a
        # fabricated ranking signal.
        candidates.sort(
            key=lambda item: (0 if item[1].is_warming else 1, item[0]),
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
        """Non-blocking warm request — ONLY when the server advertises it.

        Task 04 contract: manifests declare warm support via the
        ``capabilities`` block. If the discovered spec for ``model_id`` does
        not explicitly declare ``capabilities.warm is True``, NO request is
        issued (task-05 clause: never send unsupported warm requests to the
        inference server).
        """
        from arenamcp.magezero_client import MageZeroClient

        base_url = MageZeroClient.get_active_endpoint()
        if not base_url or cls._active_host != base_url:
            return

        spec = next((s for s in cls.cached_models() if s.model_id == model_id), None)
        if spec is None:
            logger.debug(
                "ModelZoo warm skipped: %s not in the discovered zoo "
                "(no capability data to authorize a warm request)",
                model_id,
            )
            return
        # Capability gate: explicit True only. A missing key defaults to
        # unsupported; truthy-from-default is never sufficient.
        capabilities = spec.capabilities if isinstance(spec.capabilities, dict) else {}
        if capabilities.get("warm") is not True:
            logger.debug(
                "ModelZoo warm skipped: server does not advertise warm support for %s",
                model_id,
            )
            return

        def _do_warm():
            try:
                cls._post_warm(f"{base_url.rstrip('/')}/models/{model_id}/warm")
                logger.info("Warmed model %s on inference server", model_id)
            except Exception as e:
                logger.debug("Failed to warm model %s: %s", model_id, e)

        thread = threading.Thread(target=_do_warm, daemon=True, name=f"warm-{model_id}")
        thread.start()

    @classmethod
    def _post_warm(cls, url: str, timeout: float = 2.0) -> None:
        """Issue the actual warm POST (HTTP hook for tests/audit)."""
        import urllib.request

        headers = {"Content-Type": "application/json", **cls._get_auth_headers()}
        req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 202, 204):
                raise RuntimeError(f"warm request returned HTTP {resp.status}")
