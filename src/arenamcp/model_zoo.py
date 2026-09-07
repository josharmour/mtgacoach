"""Multi-Model Model Zoo Client for MTGA Coach.

Implements the Model Zoo manifest contract, selecting the best trained neural model
for the hero's deck and format, firing non-blocking warm requests during mulligans,
and providing seamless fallback to heuristic lookahead when off-distribution.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from arenamcp.format_profile import FormatProfile
from arenamcp.magezero_gating import compute_count_weighted_jaccard

logger = logging.getLogger(__name__)


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


# Canonical default manifest for UWTempo/ver2 (bundled fallback matching training deck)
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
            url = f"{base_url}/models"
            req = urllib.request.Request(url, headers={"User-Agent": "ArenaMCP-Client/1.0"})
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models_data = data.get("models", [])
                resident_set = set(data.get("resident_model_ids", []))

                parsed = []
                for m in models_data:
                    mid = str(m.get("model_id") or "")
                    spec = ModelSpec.from_dict(m, is_resident=(mid in resident_set))
                    parsed.append(spec)

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
