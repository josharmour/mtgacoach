"""How strong is this 40-card Limited deck? A set-agnostic model fit on 17Lands games.

The model (``resources/deck_strength_model.json``, built by the dev-only
``tools/deck_benchmark``) is a logistic regression of game wins on deck features
measured *relative to the deck's own set*: card win rates (17Lands GIH) as z-scores
against the games-weighted GIH of the set's rated spells (unrated cards imputed), bombs,
removal, creature count and curve, land count, splash and fixing, and the color pair's
win rate relative to the set's pairs. It was
fit on older sets with player-skill, play/draw and mulligan controls; predictions
hold those at neutral values (median skill bucket, no mulligans, play/draw averaged),
so the number describes the deck, not the pilot. Percentiles place a deck among the
decks published to 17Lands in that format.

Pure Python, no network. Every public function tolerates unrated or unknown cards;
callers that only log the result should still guard against exceptions.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from pathlib import Path
from statistics import fmean
from typing import Any

logger = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).resolve().parent / "resources" / "deck_strength_model.json"

FEATURES = (
    "gih_mean",  # mean card z-score (GIH vs the set's games-weighted rated spells) over the spells
    "gih_top3",  # mean z of the three best spells
    "gih_bottom5",  # mean z of the five weakest spells
    "bombs",  # spells at z >= 1.5
    "removal",  # permanent answers (capped at 8)
    "tempo",  # bounce / tap / counter (capped at 6)
    "creatures",  # unconditional bodies
    "creature_short",  # max(0, 14 - creatures)
    "early_bodies",  # bodies costing <= 3 (capped at 10)
    "two_drops",  # spells costing <= 2
    "top_end",  # spells costing >= 5
    "top_end_excess",  # max(0, top_end - 4)
    "mean_cmc",
    "card_advantage",
    "evasive",  # evasive bodies
    "lands",  # land count - 17
    "land_dev",  # |land count - 17|
    "extra_cards",  # cards beyond 40
    "splash_cards",  # spells needing a color outside the main two
    "splash_colors",
    "splash_short",  # missing sources: sum over splash colors of max(0, 3 - sources)
    "duals",  # nonbasic lands making two or more of the deck's colors
    "pair_rate",  # main pair's win rate minus the set's pair average, percentage points
    "mono",  # fewer than two main colors
)

FORMAT_ALIASES = {
    "sealed": "sealed",
    "tradsealed": "sealed",
    "draft": "draft",
    "premierdraft": "draft",
    "traddraft": "draft",
    "quickdraft": "draft",
}
BOMB_Z = 1.5
Z_CLIP = 4.0
BASIC_TYPES = {"plains": "W", "island": "U", "swamp": "B", "mountain": "R", "forest": "G"}
DEFAULT_GIH_MEAN = 56.3  # typical games-weighted scale of recent sets (no ratings at all)
DEFAULT_GIH_STD = 2.8
# Unrated cards. 17Lands hides GIH below ~500 games in hand. In a fresh set those are
# mostly scarce rares and mythics, and they play worse than rated cards of their rarity
# (FRA week 1, game-present win rate: unrated mythics -2.4 points, rares -3.6). Their
# z comes from their game-present win rate when 17Lands has one (fit within the set),
# else the rated rarity mean less UNRATED_DISCOUNT, clipped to [UNRATED_FLOOR, UNRATED_CAP].
UNRATED_DISCOUNT = 1.0
UNRATED_FLOOR = -1.5
UNRATED_CAP = 0.5


@dataclass(frozen=True)
class SetContext:
    """A set's rating scale: mean/std GIH of rated spells, rarity priors, pair win rates."""

    gih_mean: float = DEFAULT_GIH_MEAN  # percentage points
    gih_std: float = DEFAULT_GIH_STD
    rarity_z: tuple[tuple[str, float], ...] = ()  # last-resort z for an unrated card, by rarity
    pair_rates: tuple[tuple[str, float], ...] = ()  # "WU" -> win rate (fraction)
    rated: int = 0
    imputed: tuple[tuple[str, float], ...] = ()  # unrated card name (lowercase) -> z from its GP win rate

    @cached_property
    def _imputed(self) -> dict[str, float]:
        return dict(self.imputed)

    def unrated_z(self, name: str, rarity: str) -> float:
        z = self._imputed.get(name.lower())
        if z is not None:
            return z
        priors = dict(self.rarity_z)
        if rarity in priors:
            return priors[rarity]
        return fmean(priors.values()) if priors else 0.0  # no set ratings: neutral

    def pair_rate_rel(self, colors: str) -> float:
        rates = dict(self.pair_rates)
        if colors not in rates or len(rates) < 3:
            return 0.0
        return 100.0 * (rates[colors] - fmean(rates.values()))


@dataclass
class DeckStrength:
    win_rate: float  # skill-neutral predicted game win rate (0-1)
    percentile: float  # 0-100 among published 17Lands decks of the format
    fmt: str
    features: dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"predicted {self.win_rate * 100:.1f}% vs 17Lands {self.fmt} decks, "
            f"{_ordinal(round(self.percentile))} percentile"
        )


def _ordinal(number: int) -> str:
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _get(card: Any, key: str, default: Any = None) -> Any:
    if isinstance(card, Mapping):
        return card.get(key, default)
    return getattr(card, key, default)


def _pct(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0 or math.isnan(number):
        return None
    return number * 100.0 if number <= 1.5 else number


def _type_line(card: Any) -> str:
    value = _get(card, "type_line") or _get(card, "types") or ""
    return " ".join(value) if isinstance(value, (list, tuple)) else str(value)


def _rarity(card: Any) -> str:
    rarity = str(_get(card, "rarity") or "").lower()
    return {"m": "mythic", "r": "rare", "u": "uncommon", "c": "common"}.get(rarity, rarity)


def _weight(card: Any) -> float:
    for key in ("gih_games", "ever_drawn_game_count", "games_in_hand", "games"):
        value = _get(card, key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return 1.0


def _game_present(card: Any) -> float | None:
    """17Lands game-present win rate (``win_rate`` in raw rows, ``game_wr`` on primer cards)."""
    for key in ("game_wr", "gp_wr", "win_rate"):
        value = _pct(_get(card, key))
        if value is not None:
            return value
    return None


def set_context(cards: Iterable[Any], pair_rates: Mapping[str, Any] | None = None) -> SetContext:
    """Context from a set's rated cards (17Lands rows, primer cards or card dicts with gih_wr).

    The scale is the GIH distribution of the cards 17Lands players actually draw:
    mean and spread weighted by games in hand (``ever_drawn_game_count`` / primer
    ``games``), so z = 0 is the card quality of a typical deck in that set. Lands are
    ignored; GIH may be a fraction (0.56) or percentage points (56.0).
    ``pair_rates`` maps two-color keys to a win rate or to a {"win_rate": ...} row.
    """
    rated: list[tuple[float, str, float, float | None]] = []
    unrated: dict[str, float] = {}
    for card in cards:
        if "land" in _type_line(card).lower():
            continue
        gih = _pct(_get(card, "gih_wr", _get(card, "ever_drawn_win_rate")))
        gp = _game_present(card)
        if gih is not None:
            rated.append((gih, _rarity(card), _weight(card), gp))
        elif gp is not None and _get(card, "name"):
            unrated[str(_get(card, "name")).lower()] = gp
    rates = {}
    for key, value in (pair_rates or {}).items():
        rate = value.get("win_rate") if isinstance(value, Mapping) else value
        try:
            rates[str(key)] = float(rate)
        except (TypeError, ValueError):
            continue
    if len(rated) < 10:
        return SetContext(pair_rates=tuple(sorted(rates.items())), rated=len(rated))
    total = sum(w for _, _, w, _ in rated)
    mean = sum(g * w for g, _, w, _ in rated) / total
    std = math.sqrt(sum(w * (g - mean) ** 2 for g, _, w, _ in rated) / total) or DEFAULT_GIH_STD
    by_rarity: dict[str, list[float]] = {}
    for gih, rarity, _w, _gp in rated:
        by_rarity.setdefault(rarity, []).append((gih - mean) / std)
    priors = tuple(
        sorted(
            (r, round(max(UNRATED_FLOOR, min(UNRATED_CAP, fmean(z) - UNRATED_DISCOUNT)), 4))
            for r, z in by_rarity.items()
            if r and len(z) >= 3
        )
    )
    # Within the set, regress card z on game-present win rate; apply it to unrated cards.
    pairs = [((gih - mean) / std, gp) for gih, _r, _w, gp in rated if gp is not None]
    imputed: tuple[tuple[str, float], ...] = ()
    if len(pairs) >= 10 and unrated:
        gp_mean = fmean(gp for _, gp in pairs)
        z_mean = fmean(z for z, _ in pairs)
        var = sum((gp - gp_mean) ** 2 for _, gp in pairs)
        slope = sum((gp - gp_mean) * (z - z_mean) for z, gp in pairs) / var if var else 0.0
        imputed = tuple(
            sorted(
                (name, round(max(-Z_CLIP, min(Z_CLIP, z_mean + slope * (gp - gp_mean))), 4))
                for name, gp in unrated.items()
            )
        )
    return SetContext(
        gih_mean=round(mean, 4),
        gih_std=round(std, 4),
        rarity_z=priors,
        pair_rates=tuple(sorted(rates.items())),
        rated=len(rated),
        imputed=imputed,
    )


def set_context_from_primer(primer: Any) -> SetContext:
    """Context from a ``set_primer.SetPrimer`` (its 17Lands card rows and pair stats)."""
    cards = [
        {
            "name": card.name,
            "gih_wr": card.gih_wr,
            "type_line": card.types,
            "rarity": card.rarity,
            "games": card.games,
            "game_wr": getattr(card, "game_wr", None),
        }
        for card in (getattr(primer, "cards", None) or {}).values()
    ]
    return set_context(cards, getattr(primer, "pair_stats", None) or {})


@dataclass(frozen=True)
class _Profile:
    land: bool
    basic: bool
    z: float
    body: bool
    removal: bool
    tempo: bool
    cmc: float
    required: frozenset[str]
    produces: frozenset[str]
    evasive: bool
    card_advantage: bool


@lru_cache(maxsize=8192)
def _rules(name: str, mana_cost: str, type_line: str, oracle_text: str, cmc: float) -> tuple:
    from arenamcp.draft_guidance import normalize_card
    from arenamcp.limited_deck import _interaction_kind, _produces, _required_colors
    from arenamcp.limited_rules import rules_profile

    card = {
        "name": name,
        "mana_cost": mana_cost,
        "type_line": type_line,
        "oracle_text": oracle_text,
        "cmc": cmc,
    }
    normalized = normalize_card(card)
    kind = _interaction_kind(card)
    return (
        bool(rules_profile(card)["unconditional_body"]),
        kind == "removal",
        kind == "tempo",
        float(normalized.cmc),
        frozenset(_required_colors(card)),
        frozenset(_produces(card)),
        "evasion" in normalized.tags,
        "card_advantage" in normalized.tags,
    )


def _profile(card: Any, ctx: SetContext) -> _Profile:
    type_line = _type_line(card)
    lowered = type_line.lower()
    try:
        cmc = float(_get(card, "cmc") or 0.0)
    except (TypeError, ValueError):
        cmc = 0.0
    body, removal, tempo, cmc, required, produces, evasive, draws = _rules(
        str(_get(card, "name") or ""),
        str(_get(card, "mana_cost") or ""),
        type_line,
        str(_get(card, "oracle_text") or ""),
        cmc,
    )
    basic = "basic" in lowered and "land" in lowered
    if basic:
        produces = produces | {color for word, color in BASIC_TYPES.items() if word in lowered}
    gih = _pct(_get(card, "gih_wr"))
    if gih is not None:
        z = max(-Z_CLIP, min(Z_CLIP, (gih - ctx.gih_mean) / (ctx.gih_std or DEFAULT_GIH_STD)))
    else:
        z = ctx.unrated_z(str(_get(card, "name") or ""), _rarity(card))
    return _Profile(
        land="land" in lowered,
        basic=basic,
        z=z,
        body=body,
        removal=removal,
        tempo=tempo,
        cmc=cmc,
        required=required,
        produces=produces,
        evasive=evasive,
        card_advantage=draws,
    )


def _main_colors(spells: Sequence[_Profile]) -> tuple[str, tuple[str, ...]]:
    needed = Counter(color for card in spells for color in card.required)
    ranked = [
        color for color, _ in sorted(needed.items(), key=lambda item: (-item[1], "WUBRG".index(item[0])))
    ]
    main = "".join(sorted(ranked[:2], key="WUBRG".index))
    return main, tuple(sorted(ranked[2:], key="WUBRG".index))


def deck_features(
    cards: Sequence[Any], ctx: SetContext | None = None, basic_lands: Mapping[str, int] | None = None
) -> dict[str, float]:
    """Set-relative features of one deck.

    ``cards`` lists every main-deck card, one entry per copy (dicts like the deck
    builder's pool cards: name, mana_cost, type_line, oracle_text, rarity, gih_wr).
    Basic lands may be included as cards or given as ``basic_lands`` ({"W": 8, ...}).
    """
    ctx = ctx or SetContext()
    profiles = [_profile(card, ctx) for card in cards]
    spells = [p for p in profiles if not p.land]
    lands = [p for p in profiles if p.land]
    basics = Counter({color: int(count) for color, count in (basic_lands or {}).items() if count})
    land_count = len(lands) + sum(basics.values())
    zs = sorted((p.z for p in spells), reverse=True)
    main, splash = _main_colors(spells)
    colors = set(main) | set(splash)
    sources: Counter = Counter()
    for color, count in basics.items():
        sources[color] += count
    for profile in profiles:
        for color in profile.produces & colors:
            sources[color] += 1
    bodies = [p for p in spells if p.body]
    top_end = sum(p.cmc >= 5 for p in spells)
    return {
        "gih_mean": fmean(zs) if zs else 0.0,
        "gih_top3": fmean(zs[:3]) if zs else 0.0,
        "gih_bottom5": fmean(zs[-5:]) if zs else 0.0,
        "bombs": float(sum(z >= BOMB_Z for z in zs)),
        "removal": float(min(8, sum(p.removal for p in spells))),
        "tempo": float(min(6, sum(p.tempo for p in spells))),
        "creatures": float(len(bodies)),
        "creature_short": float(max(0, 14 - len(bodies))),
        "early_bodies": float(min(10, sum(p.cmc <= 3 for p in bodies))),
        "two_drops": float(sum(p.cmc <= 2 for p in spells)),
        "top_end": float(top_end),
        "top_end_excess": float(max(0, top_end - 4)),
        "mean_cmc": fmean(p.cmc for p in spells) if spells else 0.0,
        "card_advantage": float(sum(p.card_advantage for p in spells)),
        "evasive": float(sum(p.evasive for p in bodies)),
        "lands": float(land_count - 17),
        "land_dev": float(abs(land_count - 17)),
        "extra_cards": float(max(0, len(spells) + land_count - 40)),
        "splash_cards": float(sum(bool(p.required & set(splash)) for p in spells)),
        "splash_colors": float(len(splash)),
        "splash_short": float(sum(max(0, 3 - sources[color]) for color in splash)),
        "duals": float(sum(not p.basic and len(p.produces & colors) >= 2 for p in lands)),
        "pair_rate": ctx.pair_rate_rel(main) if len(main) == 2 else 0.0,
        "mono": float(len(main) < 2),
    }


@lru_cache(maxsize=4)
def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_model(path: Path | str | None = None) -> dict:
    return _load(str(path or MODEL_PATH))


def _format_model(fmt: str, model: Mapping | None) -> tuple[str, Mapping]:
    model = model or load_model()
    key = FORMAT_ALIASES.get(str(fmt or "draft").lower(), "draft")
    formats = model["formats"]
    return key, formats.get(key) or formats["draft"]


def predict_win_rate(
    features: Mapping[str, float], fmt: str = "draft", model: Mapping | None = None
) -> float:
    """Skill-neutral game win rate (0-1) for a deck's features."""
    _key, params = _format_model(fmt, model)
    eta = 0.0
    for name, mean, scale, coef in zip(
        params["features"], params["mean"], params["scale"], params["coef"], strict=True
    ):
        value = float(features.get(name, mean))
        eta += coef * (value - mean) / (scale or 1.0)
    play = 1.0 / (1.0 + math.exp(-(params["intercept_play"] + eta)))
    draw = 1.0 / (1.0 + math.exp(-(params["intercept_draw"] + eta)))
    return 0.5 * (play + draw)


def percentile(win_rate: float, fmt: str = "draft", model: Mapping | None = None) -> float:
    """0-100: share of published 17Lands decks of this format predicted below ``win_rate``."""
    _key, params = _format_model(fmt, model)
    table = params["percentiles"]  # predicted win rate at 0, 1, ..., 100 percent
    if win_rate <= table[0]:
        return 0.0
    if win_rate >= table[-1]:
        return 100.0
    index = bisect.bisect_right(table, win_rate) - 1
    low, high = table[index], table[index + 1]
    step = 100.0 / (len(table) - 1)
    return step * (index + ((win_rate - low) / (high - low) if high > low else 0.0))


def evaluate_deck(
    cards: Sequence[Any],
    ctx: SetContext | None = None,
    fmt: str = "draft",
    basic_lands: Mapping[str, int] | None = None,
    model: Mapping | None = None,
) -> DeckStrength:
    features = deck_features(cards, ctx, basic_lands)
    key, _params = _format_model(fmt, model)
    win_rate = predict_win_rate(features, key, model)
    return DeckStrength(win_rate, percentile(win_rate, key, model), key, features)


def evaluate_build(
    build: Mapping[str, Any],
    pool: Sequence[Mapping[str, Any]],
    ctx: SetContext | None = None,
    fmt: str = "draft",
    model: Mapping | None = None,
) -> DeckStrength:
    """Score a limited_deck build ({"main_deck": [{grp_id, count}], "basic_lands": {...}})."""
    by_id = {card.get("grp_id"): card for card in pool}
    cards = [
        by_id[entry["grp_id"]]
        for entry in build.get("main_deck") or []
        if entry.get("grp_id") in by_id
        for _ in range(int(entry.get("count") or 1))
    ]
    return evaluate_deck(cards, ctx, fmt, build.get("basic_lands") or {}, model)


def _label(build: Mapping[str, Any]) -> str:
    quality = build.get("quality") or {}
    colors = str(quality.get("colors") or "")
    return colors + (f"+{quality['splash']}" if quality.get("splash") else "")


def log_build_strength(
    build: Mapping[str, Any],
    options: Sequence[Mapping[str, Any]] | None,
    pool: Sequence[Mapping[str, Any]],
    fmt: str,
    primer: Any = None,
) -> list[str]:
    """Log the chosen build's (and each option's) predicted strength. Diagnostics only: never raises.

    The scale comes from the primer's 17Lands ratings; without one, a generic scale.
    Returns the logged lines.
    """
    lines: list[str] = []
    try:
        ctx = set_context_from_primer(primer) if primer is not None else SetContext()
        source = f"{ctx.rated} rated cards" if ctx.rated >= 10 else "no set ratings, generic scale"
        chosen = evaluate_build(build, pool, ctx, fmt)
        f = chosen.features
        lines.append(
            f"Deck strength ({chosen.fmt}): {chosen.summary()} [{_label(build) or '?'}; card quality "
            f"z {f['gih_mean']:+.2f}, {int(f['splash_colors'])} splash color(s), {int(f['creatures'])} creatures, "
            f"{int(f['removal'])} removal, {int(f['lands']) + 17} lands; {source}]"
        )
        for index, option in enumerate(options or [], 1):
            strength = evaluate_build(option, pool, ctx, fmt)
            lines.append(f"Deck option {index} ({_label(option) or '?'}): {strength.summary()}")
    except Exception as exc:  # noqa: BLE001 - logging must never block deck submission
        lines.append(f"Deck strength unavailable: {type(exc).__name__}: {exc}")
    for line in lines:
        logger.info(line)
    return lines
