"""Platform-independent draft guidance engine ("pane of glass" backend).

Pure-function ratings/reasons layer: given the cards in the current pack
(with 17lands stats), the current pool, and the pick/pack position, produce a
ranked list of :class:`PickGuidance` records that BOTH the guidance overlay
and the voice coach can consume. Every scoring factor emits a human-readable
reason string — the reasons ARE the product (they get displayed in the
overlay table and narrated by TTS).

Design constraints:
- **Pure**: no I/O, no network, no globals mutated. Feed it normalized
  :class:`CardData`; get back sorted guidance. All the fetching/caching
  stays in ``draftstats.py`` / ``scryfall.py`` / ``server.py``.
- **Dependency-light**: stdlib only (``statistics``/``re``) — a 15-card
  pack does not need numpy.
- **Source-agnostic inputs**: :func:`normalize_card` adapts our own shapes
  (``draftstats.DraftStats``, ``scryfall.ScryfallCard``, the enriched card
  dicts built by ``server.get_draft_pack``) into one internal shape, so the
  engine never cares where the data came from.

Algorithm attribution: the highest-value ideas are ported (simplified, with
notes at each site) from the unrealities MTGA_Draft_17Lands advisor —
``src/advisor/engine.py`` (compositional VALUE score + reason strings),
``src/signals.py`` (open-lane lateness signals), ``src/set_metrics.py``
(format texture), ``src/constants.py`` (wheel polynomial coefficients).
Repo: https://github.com/unrealities/MTGA_Draft_17Lands
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "ArchStats",
    "CardData",
    "FormatContext",
    "PickGuidance",
    "PoolNeeds",
    "LaneState",
    "normalize_card",
    "format_context_from_pair_stats",
    "compute_lane",
    "analyze_pool",
    "compute_pack_signals",
    "evaluate_pack",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COLOR_LETTERS = ("W", "U", "B", "R", "G")

COLOR_NAMES = {
    "W": "white",
    "U": "blue",
    "B": "black",
    "R": "red",
    "G": "green",
}

# MTGA Premier Draft packs contain 14 cards (pick 15 is auto in paper only).
DEFAULT_PICKS_PER_PACK = 14
PACKS_PER_DRAFT = 3

# Composition targets — ported from unrealities engine.py class constants
# (DraftAdvisor.TARGET_*). These are the classic BREAD/limited heuristics:
# ~14 creatures, ~7 early plays, ~3 pieces of hard removal in a 45-card pool.
TARGET_CREATURES = 13
TARGET_EARLY_PLAYS = 7
TARGET_HARD_REMOVAL = 3
REMOVAL_SATURATION = 6
HEAVY_DROP_CAP = 4

# Bomb thresholds — unrealities engine.py: BOMB_Z_SCORE / IWD_PREMIUM_THRESHOLD.
# IWD here is in percentage points (17lands serves a fraction; we scale by 100).
BOMB_Z_SCORE = 1.5
IWD_PREMIUM_PCT = 4.5

# Bayesian archetype-blend knobs — unrealities engine.py _calculate_weighted_score.
ARCH_SAMPLE_FULL_CONFIDENCE = 1000  # games at which pair stats are fully trusted
ARCH_SAMPLE_MIN = 10                # below this, ignore pair stats entirely

# Wheel-probability cubic polynomials, one per pick 1..6 (index pick-1,
# clamped), evaluated at ALSA. Ported verbatim from unrealities
# constants.py:792 WHEEL_COEFFICIENTS (their fit of 17lands wheel data).
WHEEL_COEFFICIENTS = (
    (-0.46, 7.97, -27.43, 26.61),
    (-0.33, 6.31, -23.12, 23.86),
    (-0.19, 4.39, -17.06, 17.71),
    (-0.06, 2.27, -9.22, 9.43),
    (0.08, 0.15, -1.88, 2.36),
    (0.25, -2.65, 9.76, -11.21),
)

# Tier thresholds on the 0-100 guidance score. Mirrors
# arenamcp.draft_eval.TIER_THRESHOLDS so overlay badges stay consistent with
# the existing HUD tiers (kept local so this module stays import-light).
TIER_THRESHOLDS = (
    (85, "FIRE"),
    (70, "GOLD"),
    (55, "SILVER"),
    (45, "BRONZE"),
    (0, "WEAK"),
)


def get_tier(score: float) -> str:
    """Map a 0-100 guidance score to a tier label."""
    for threshold, label in TIER_THRESHOLDS:
        if score >= threshold:
            return label
    return "WEAK"


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchStats:
    """Per-archetype (color pair) 17lands stats for one card.

    Our current ``draftstats.py`` only fetches global ("All Decks") card
    ratings; per-pair card stats need the filtered ``/api/card_data`` route
    (the old ``/card_ratings/data`` route silently ignores filters — see
    docs/DECISIONS.md). The engine blends these in when present and degrades
    to global-only when absent, so the fetch can land later without engine
    changes.
    """

    gih_wr_pct: float  # GIH win rate in percentage points (e.g. 57.2)
    games: int = 0     # sample size backing that win rate


@dataclass(frozen=True)
class CardData:
    """Normalized card input — the only shape the engine understands."""

    grp_id: int
    name: str
    colors: tuple[str, ...] = ()          # subset of WUBRG
    mana_cost: str = ""                   # "{1}{W}{W}" style, may be ""
    cmc: float = 0.0
    types: tuple[str, ...] = ()           # ("Creature", "Human", ...)
    rarity: str = ""                      # "common"/"uncommon"/"rare"/"mythic"
    tags: frozenset[str] = frozenset()    # "removal", "evasion", "card_advantage", "fixing"
    gih_wr_pct: Optional[float] = None    # GIH WR in percentage points (54.0)
    alsa: Optional[float] = None          # average last seen at
    iwd_pct: Optional[float] = None       # improvement-when-drawn, pct points
    games: int = 0                        # GIH sample size
    arch_stats: Mapping[str, ArchStats] = field(default_factory=dict)  # "WU" -> stats

    @property
    def is_creature(self) -> bool:
        return "Creature" in self.types

    @property
    def is_land(self) -> bool:
        return "Land" in self.types

    @property
    def is_basic_land(self) -> bool:
        return "Basic" in self.types and "Land" in self.types


@dataclass(frozen=True)
class FormatContext:
    """Format-level texture: baselines and pair win rates.

    ``mean_gih_wr_pct``/``std_gih_wr_pct`` are the set-wide GIH WR mean/std
    (unrealities set_metrics.py). Defaults are the same fallbacks unrealities
    uses when the dataset is empty (54.0 / 4.0).
    """

    mean_gih_wr_pct: float = 54.0
    std_gih_wr_pct: float = 4.0
    pair_win_rates: Mapping[str, float] = field(default_factory=dict)  # "WU" -> 0.556


@dataclass
class LaneState:
    """Where the drafter is committed, with soft weights per color."""

    main_colors: tuple[str, ...] = ()          # up to 3, strongest first
    weights: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def pair_key(self) -> str:
        """Top-two lane colors in WUBRG order ('WU'), or '' if uncommitted."""
        top2 = self.main_colors[:2]
        if len(top2) < 2:
            return ""
        return "".join(c for c in COLOR_LETTERS if c in top2)

    def lane_phrase(self) -> str:
        """Spoken name for the lane, e.g. 'white-blue'."""
        top2 = self.main_colors[:2]
        if not top2:
            return ""
        return "-".join(COLOR_NAMES.get(c, c) for c in top2)


@dataclass
class PoolNeeds:
    """Composition census of the current pool."""

    creatures: int = 0
    early_plays: int = 0          # cmc<=2 creatures + cheap non-creature removal
    removal: int = 0
    heavy_drops: int = 0          # cmc>=5 nonland
    fixing: int = 0               # nonbasic lands with 2+ colors / fixing tag
    splash_targets: set[str] = field(default_factory=set)  # off-lane bomb colors


@dataclass
class PickGuidance:
    """One ranked recommendation — the record both consumers render."""

    grp_id: int
    name: str
    score: float                       # 0-100
    tier: str                          # WEAK/BRONZE/SILVER/GOLD/FIRE
    reasons: list[str]                 # human-readable, voice-narratable
    wheel_probability: float           # 0-100 (% chance this card wheels)
    gih_wr_pct: Optional[float] = None
    z_score: float = 0.0               # pack-relative power
    is_bomb: bool = False
    on_lane: bool = True
    lane: str = ""                     # committed pair key ("WU") or ""


# ---------------------------------------------------------------------------
# Normalization — adapt our data sources into CardData
# ---------------------------------------------------------------------------

_PIP_RE = re.compile(r"\{(.*?)\}")


def _colors_from_mana_cost(mana_cost: str) -> tuple[str, ...]:
    """Derive color identity from mana pips (handles hybrid '{W/U}')."""
    found: list[str] = []
    for pip in _PIP_RE.findall(mana_cost or ""):
        for part in pip.split("/"):
            part = part.strip().upper()
            if part in COLOR_LETTERS and part not in found:
                found.append(part)
    return tuple(c for c in COLOR_LETTERS if c in found)


def _types_from_type_line(type_line: str) -> tuple[str, ...]:
    """Split 'Legendary Creature — Human Wizard' into word tokens."""
    if not type_line:
        return ()
    cleaned = type_line.replace("—", " ").replace("-", " ")
    return tuple(w for w in cleaned.split() if w)


# Heuristic tag derivation. The unrealities dataset ships curated per-card
# tags from their scryfall_tagger; we don't have that pipeline, so derive the
# handful the engine actually uses from oracle text (same spirit as our
# draft_eval.get_card_type_score keyword scan).
_REMOVAL_WORDS = ("destroy target", "exile target", "damage to any target",
                  "damage to target creature", "fights", "-x/-x",
                  "destroy all", "deals damage equal")
_EVASION_WORDS = ("flying", "menace", "trample", "can't be blocked", "shadow")
_DRAW_WORDS = ("draw a card", "draw two", "draw cards")
_FIXING_WORDS = ("add one mana of any color", "search your library for a basic land",
                 "any color of mana")


def _derive_tags(type_line: str, oracle_text: str, colors: tuple[str, ...]) -> frozenset[str]:
    tags: set[str] = set()
    tl = (type_line or "").lower()
    ot = (oracle_text or "").lower()
    if any(w in ot for w in _REMOVAL_WORDS):
        tags.add("removal")
    if any(w in ot for w in _EVASION_WORDS):
        tags.add("evasion")
    if any(w in ot for w in _DRAW_WORDS):
        tags.add("card_advantage")
    if any(w in ot for w in _FIXING_WORDS):
        tags.add("fixing")
    if "land" in tl and "basic" not in tl and len(colors) != 1:
        tags.add("fixing")
    return frozenset(tags)


def _as_pct(value: Any) -> Optional[float]:
    """Accept a win rate as fraction (0.57) or percentage (57.0) → pct points.

    17lands / our draftstats serve fractions; unrealities datasets store
    percentages. 1.5 is a safe cut line — no limited card has a 1.5% GIH WR.
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v * 100.0 if v <= 1.5 else v


def _get(source: Any, key: str, default: Any = None) -> Any:
    """Read `key` from a mapping or an attribute from an object."""
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def normalize_card(
    source: Any,
    stats: Any = None,
    *,
    arch_stats: Optional[Mapping[str, ArchStats]] = None,
) -> CardData:
    """Adapt any of our card shapes into :class:`CardData`.

    Accepts:
    - the enriched card dicts built by ``server.get_draft_pack`` /
      ``enrich_with_oracle_text`` (keys: grp_id, name, oracle_text,
      type_line, mana_cost, optionally gih_wr/alsa/iwd/colors/cmc/rarity);
    - a ``scryfall.ScryfallCard`` (attributes of the same names) as
      ``source`` plus a ``draftstats.DraftStats`` as ``stats``;
    - any mapping/object with a compatible subset — everything is optional.

    ``stats`` (a ``DraftStats``-like) overrides win-rate fields when given.
    ``arch_stats`` attaches per-pair stats ("WU" → :class:`ArchStats`) for
    the Bayesian archetype blend.
    """
    grp_id = int(_get(source, "grp_id", None) or _get(source, "arena_id", 0) or 0)
    name = str(_get(source, "name", "") or f"Unknown ({grp_id})")
    mana_cost = str(_get(source, "mana_cost", "") or "")
    type_line = str(_get(source, "type_line", "") or "")
    oracle_text = str(_get(source, "oracle_text", "") or "")
    rarity = str(_get(source, "rarity", "") or "").lower()

    raw_colors = _get(source, "colors", None)
    if raw_colors:
        colors = tuple(c for c in COLOR_LETTERS
                       if c in {str(x).upper() for x in raw_colors})
    else:
        colors = _colors_from_mana_cost(mana_cost)

    try:
        cmc = float(_get(source, "cmc", 0.0) or 0.0)
    except (TypeError, ValueError):
        cmc = 0.0

    # Win-rate fields: prefer the explicit stats object, fall back to the
    # source dict's own keys (get_draft_pack embeds gih_wr/alsa/iwd).
    gih = _as_pct(_get(stats, "gih_wr", None) if stats is not None else None)
    if gih is None:
        gih = _as_pct(_get(source, "gih_wr", None))
    alsa = _get(stats, "alsa", None) if stats is not None else None
    if alsa is None:
        alsa = _get(source, "alsa", None)
    try:
        alsa = float(alsa) if alsa is not None else None
    except (TypeError, ValueError):
        alsa = None
    iwd = _as_pct(_get(stats, "iwd", None) if stats is not None else None)
    if iwd is None:
        iwd = _as_pct(_get(source, "iwd", None))
    games = _get(stats, "games_in_hand", None) if stats is not None else None
    if games is None:
        games = _get(source, "games_in_hand", 0) or _get(source, "games", 0) or 0

    types = _types_from_type_line(type_line)
    tags = _derive_tags(type_line, oracle_text, colors)
    # Allow callers to pass pre-computed tags through (e.g. curated datasets).
    extra_tags = _get(source, "tags", None)
    if extra_tags:
        tags = tags | frozenset(str(t) for t in extra_tags)

    return CardData(
        grp_id=grp_id,
        name=name,
        colors=colors,
        mana_cost=mana_cost,
        cmc=cmc,
        types=types,
        rarity=rarity,
        tags=tags,
        gih_wr_pct=gih,
        alsa=alsa,
        iwd_pct=iwd,
        games=int(games or 0),
        arch_stats=dict(arch_stats or {}),
    )


def format_context_from_pair_stats(pair_stats: Mapping[str, Any],
                                   *,
                                   mean_gih_wr_pct: float = 54.0,
                                   std_gih_wr_pct: float = 4.0) -> FormatContext:
    """Build a :class:`FormatContext` from ``draftstats.get_color_pair_stats``.

    Accepts the ``{"WU": ColorPairStats(...)}`` dict our cache returns (any
    object with a ``win_rate`` attribute, or plain floats).
    """
    rates: dict[str, float] = {}
    for key, val in (pair_stats or {}).items():
        wr = _get(val, "win_rate", None)
        if wr is None and isinstance(val, (int, float)):
            wr = float(val)
        if wr is not None:
            rates[str(key)] = float(wr)
    return FormatContext(mean_gih_wr_pct=mean_gih_wr_pct,
                         std_gih_wr_pct=std_gih_wr_pct,
                         pair_win_rates=rates)


# ---------------------------------------------------------------------------
# Lane / pool analysis
# ---------------------------------------------------------------------------


def compute_lane(pool: Sequence[CardData], fmt: FormatContext) -> LaneState:
    """Identify the drafter's committed colors with recency bias.

    Port of unrealities engine.py ``_identify_main_colors``: each playable
    pool card contributes weight to its colors proportional to how far above
    the format mean it is, scaled up to 3x for the most recent picks (a lane
    change mid-draft should out-vote stale early picks).
    """
    weights = {c: 0.0 for c in COLOR_LETTERS}
    counts = {c: 0 for c in COLOR_LETTERS}
    playable_floor = fmt.mean_gih_wr_pct - fmt.std_gih_wr_pct
    n = len(pool)

    for idx, card in enumerate(pool):
        if not card.is_land:
            for c in card.colors:
                counts[c] += 1
        wr = card.gih_wr_pct
        if wr is None or wr < playable_floor:
            continue
        base_points = max(
            0.2, 1.0 + 2.0 * ((wr - fmt.mean_gih_wr_pct) / max(0.1, fmt.std_gih_wr_pct))
        )
        recency_mult = 1.0 + 2.0 * (idx / max(1, n))  # oldest 1.0x → newest ~3.0x
        for c in card.colors:
            weights[c] += base_points * recency_mult

    sorted_w = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
    main: list[str] = []
    total_pips = sum(counts.values())
    if n >= 15 and total_pips > 5:
        # Established pool: main colors = the two most-counted colors plus
        # anything holding >=15% of pips (allows a committed 3-color lane).
        threshold = total_pips * 0.15
        leaders = [c for c, cnt in sorted(counts.items(), key=lambda kv: kv[1],
                                          reverse=True)[:2] if cnt > 0]
        for c, w in sorted_w:
            if c in leaders or counts[c] >= threshold:
                main.append(c)
    else:
        # Early draft: only colors with real accumulated quality count.
        for c, w in sorted_w:
            if w >= 2.5:
                main.append(c)

    return LaneState(main_colors=tuple(main[:3]), weights=weights, counts=counts)


def analyze_pool(pool: Sequence[CardData], fmt: FormatContext,
                 lane: LaneState) -> PoolNeeds:
    """Census the pool for composition pressure (unrealities ``_analyze_pool``,
    trimmed to the roles our tag derivation can actually see)."""
    needs = PoolNeeds()
    bomb_floor = fmt.mean_gih_wr_pct + 1.5 * fmt.std_gih_wr_pct
    main = set(lane.main_colors)

    for card in pool:
        if card.is_creature:
            needs.creatures += 1
            if card.cmc <= 2:
                needs.early_plays += 1
        if card.cmc >= 5 and not card.is_land:
            needs.heavy_drops += 1
        if "removal" in card.tags:
            needs.removal += 1
            if card.cmc <= 2 and not card.is_creature:
                needs.early_plays += 1
        if "fixing" in card.tags or (card.is_land and len(card.colors) > 1):
            needs.fixing += 1
        # Off-lane bombs already in the pool define splash targets: fixing
        # that enables casting them gets a composition boost.
        wr = card.gih_wr_pct
        if wr is not None and wr > bomb_floor and main:
            for c in card.colors:
                if c not in main:
                    needs.splash_targets.add(c)

    return needs


def compute_pack_signals(pack: Sequence[CardData], pick_number: int,
                         fmt: FormatContext) -> dict[str, float]:
    """Open-lane lateness signal per color from the current pack.

    Port of unrealities signals.py ``calculate_pack_signals``: an
    above-baseline card still in the pack later than its ALSA says it should
    be scores ``lateness * quality`` for its colors. Callers may accumulate
    these across picks and pass the running total to :func:`evaluate_pack`.
    """
    signals = {c: 0.0 for c in COLOR_LETTERS}
    for card in pack:
        wr, alsa = card.gih_wr_pct, card.alsa
        if wr is None or alsa is None or alsa <= 0:
            continue
        if wr <= fmt.mean_gih_wr_pct:
            continue
        lateness = pick_number - alsa
        if lateness <= 0:
            continue
        score = lateness * (wr - fmt.mean_gih_wr_pct)
        for c in card.colors or ():
            signals[c] += score
    return signals


# ---------------------------------------------------------------------------
# Scoring factors (each returns value + optional reason string)
# ---------------------------------------------------------------------------


def _polyval(coeffs: Sequence[float], x: float) -> float:
    """Horner's method — replaces np.polyval, highest-order term first."""
    result = 0.0
    for c in coeffs:
        result = result * x + c
    return result


def _blended_base_score(card: CardData, lane_pair: str, overall_pick: int,
                        total_picks: int, fmt: FormatContext) -> tuple[float, Optional[str]]:
    """Base 0-100ish score from Bayesian-smoothed global/archetype blend.

    Port of unrealities engine.py ``_calculate_weighted_score``: the weight
    on pair-specific stats slides from 0.2 (P1P1 — trust the format) to 0.9
    (last picks — trust your lane) across the draft, and the pair win rate is
    itself shrunk toward the global rate when its sample is small
    (confidence = games/1000, capped at 1).
    """
    global_wr = card.gih_wr_pct
    if global_wr is None:
        # No data: park it at the format mean so type/composition factors
        # still differentiate. Callers see the reason string.
        return 50.0, "No 17lands data yet"

    reason = None
    blended = global_wr
    arch = card.arch_stats.get(lane_pair) if lane_pair else None
    if arch is not None and arch.games >= ARCH_SAMPLE_MIN and arch.gih_wr_pct > 0:
        progress = overall_pick / max(1, total_picks)
        arch_weight = min(0.9, 0.2 + progress * 0.7)
        confidence = min(1.0, arch.games / ARCH_SAMPLE_FULL_CONFIDENCE)
        trusted = arch.gih_wr_pct * confidence + global_wr * (1.0 - confidence)
        blended = global_wr * (1.0 - arch_weight) + trusted * arch_weight
        delta = trusted - global_wr
        if delta >= 1.0:
            reason = (f"Overperforms in {lane_pair} decks "
                      f"(+{delta:.1f} points over its global rate)")
        elif delta <= -1.0:
            reason = f"Weaker in {lane_pair} decks than its global rate suggests"

    score = 50.0 + ((blended - fmt.mean_gih_wr_pct)
                    / max(0.1, fmt.std_gih_wr_pct)) * 15.0
    return max(0.0, score), reason


def _bomb_bonus(card: CardData, pack_mean: float, pack_std: float
                ) -> tuple[float, float, bool, list[str]]:
    """Pack-relative bomb detection (unrealities engine.py STEP 2).

    Returns (bonus, z_score, is_true_bomb, reasons). z is computed against
    THIS pack's win-rate distribution — a 58% card is a bomb in a weak pack
    and merely good in a strong one.
    """
    wr = card.gih_wr_pct
    if wr is None:
        return 0.0, 0.0, False, []
    z = (wr - pack_mean) / max(0.1, pack_std)
    reasons: list[str] = []
    true_bomb = (card.iwd_pct is not None and card.iwd_pct > IWD_PREMIUM_PCT
                 and z > 1.0)
    iwd_mult = 1.15 if true_bomb else 1.0
    bonus = max(0.0, z * 10.0 * iwd_mult) if z > 0.5 else 0.0
    if z >= BOMB_Z_SCORE:
        reasons.append("Elite bomb — far above this pack's power level")
    elif z > 0.75:
        reasons.append("Clearly the strongest tier of this pack")
    if true_bomb:
        reasons.append("True bomb — wins the games it's drawn in")
    return bonus, z, true_bomb, reasons


def _late_signal_bonus(card: CardData, z: float, pack_number: int,
                       pick_number: int) -> tuple[float, Optional[str]]:
    """A strong card far past its ALSA in pack 1 = the lane is being passed
    to us (unrealities engine.py STEP 3)."""
    if pack_number != 1 or pick_number < 5:
        return 0.0, None
    alsa = card.alsa
    if alsa is None or alsa <= 0:
        return 0.0, None
    lateness = pick_number - alsa
    if lateness >= 2.0 and z > 0.5:
        colors = " and ".join(COLOR_NAMES.get(c, c) for c in card.colors) or "its colors"
        return (
            lateness * z * 3.0,
            f"Late signal — usually gone by pick {alsa:.0f}, still here at "
            f"pick {pick_number}. {colors.capitalize()} looks open",
        )
    return 0.0, None


def _lane_fit(card: CardData, lane: LaneState, pack_number: int,
              on_color_pool_count: int) -> tuple[float, bool, Optional[str]]:
    """On-lane multiplier (unrealities STEP 4, minus their curated-tag glue
    detection). Returns (multiplier, on_lane, reason)."""
    main = set(lane.main_colors)
    if len(main) < 2:
        return 1.0, True, None  # not committed yet — stay open, no bias
    on_lane = all(c in main for c in card.colors) if card.colors else True
    if not on_lane:
        return 1.0, False, None  # castability handles the penalty + reason
    needs_playables = pack_number == PACKS_PER_DRAFT and on_color_pool_count < 20
    mult = 1.3 if needs_playables else 1.1
    phrase = lane.lane_phrase()
    if needs_playables:
        return mult, True, f"Fits your {phrase} deck — and you still need playables"
    if card.colors:  # don't narrate lane fit for colorless
        return mult, True, f"Fits your {phrase} lane"
    return mult, True, None


def _castability(card: CardData, lane: LaneState, needs: PoolNeeds,
                 pack_number: int, pick_number: int, z: float
                 ) -> tuple[float, Optional[str]]:
    """Castability pressure by pick number (unrealities STEP 6 /
    ``_calculate_castability_v5``, simplified: no per-color fixing map — we
    use the pool's total fixing count).

    Early pack 1: speculation is nearly free. By pack 3 an off-color card is
    almost worthless unless it's a bomb you can actually splash.
    """
    top2 = set(lane.main_colors[:2])
    if len(top2) < 2:
        return 1.0, None  # uncommitted — everything is "on lane"

    # Count off-color pips from the mana cost (hybrid pips count as on-color
    # if any half is castable in-lane).
    off_pips = 0
    if card.mana_cost:
        for pip in _PIP_RE.findall(card.mana_cost):
            opts = [p for p in pip.split("/") if p in COLOR_LETTERS]
            if opts and not any(o in top2 for o in opts):
                off_pips += 1
        on_lane = off_pips == 0
    else:
        on_lane = all(c in top2 for c in card.colors) if card.colors else True
        off_pips = 0 if on_lane else 1

    if on_lane:
        return 1.0, None

    lane_phrase = lane.lane_phrase()

    if pack_number == 1:
        # Gentle, growing pressure: picks 1-8 are free, then -5% per pick.
        pressure = 1.0 - max(0, pick_number - 8) * 0.05
        if len(card.colors) > 1 and off_pips > 0:
            return max(0.2, pressure - 0.2), "Off-color gold card — a real commitment"
        return max(0.4, pressure), f"Off-color for your developing {lane_phrase} lane"

    # Packs 2-3: committed.
    if off_pips >= 2 and needs.fixing < 2:
        return 0.01, "Nearly uncastable — double off-color pips and no fixing"

    is_premium_removal = "removal" in card.tags and z >= 1.0
    if z >= BOMB_Z_SCORE or is_premium_removal:
        if off_pips == 1 and needs.fixing >= (4 if pack_number == 3 else 3):
            what = "Bomb" if z >= BOMB_Z_SCORE else "Premium removal"
            return ((0.35 if pack_number == 3 else 0.45),
                    f"{what} splash — your fixing can support the stretch")

    if off_pips == 1 and needs.fixing >= 2:
        return 0.3, "Splashable with your fixing, but off your main colors"

    return ((0.02 if pack_number == 3 else 0.05),
            f"Off-color — you're committed to {lane_phrase}")


def _composition(card: CardData, needs: PoolNeeds, pack_number: int,
                 pool_size: int, total_picks: int) -> tuple[float, Optional[str]]:
    """Composition pressure: what does the DECK need right now?
    (unrealities STEP 7 / ``_calculate_composition_bonus``, minus their
    curated synergy tags.) First matching rule wins, like the original."""
    # 1. Curve too heavy
    if card.cmc >= 5 and needs.heavy_drops >= HEAVY_DROP_CAP and not card.is_land:
        return 0.7, (f"Curve is getting top-heavy — you already have "
                     f"{needs.heavy_drops} five-plus drops")

    # 2. Creature quota (projected to end of draft)
    if pack_number >= 2 and card.is_creature:
        projected = needs.creatures * (total_picks / max(1, pool_size))
        if projected < TARGET_CREATURES:
            return 1.25, (f"You need creatures — only {needs.creatures} so far, "
                          f"on pace for {projected:.0f}")

    # 3. Fixing that enables a bomb splash already in the pool
    if (card.is_land or "fixing" in card.tags) and needs.splash_targets:
        if any(c in needs.splash_targets for c in card.colors) or not card.colors:
            return 1.3, "Fixing that lets you splash the bomb already in your pool"

    # 4. Removal quota / saturation
    if "removal" in card.tags:
        if pack_number >= 2 and needs.removal < TARGET_HARD_REMOVAL:
            return 1.3, (f"You still need removal — only {needs.removal} "
                         f"piece{'s' if needs.removal != 1 else ''} so far")
        if needs.removal > REMOVAL_SATURATION:
            return 0.8, f"Removal saturated — you already have {needs.removal}"

    # 5. Early plays / two-drops
    if card.cmc <= 2 and (card.is_creature or "removal" in card.tags):
        projected = needs.early_plays * (total_picks / max(1, pool_size))
        if projected < TARGET_EARLY_PLAYS:
            if pack_number >= 2:
                boost = 1.0 + min(0.5, (TARGET_EARLY_PLAYS - projected) * 0.15)
                return boost, (f"Two-drops badly needed — only "
                               f"{needs.early_plays} early plays in the pool")
            return 1.1, "Good curve foundation — early plays win limited games"

    return 1.0, None


def _wheel_probability(card: CardData, pick_number: int, rank_in_pack: int
                       ) -> tuple[float, float, Optional[str]]:
    """ALSA-based wheel estimate (unrealities ``_check_relative_wheel``).

    The cubic polynomial (fit on 17lands wheel data, constants.py:792 in the
    unrealities repo) maps ALSA → wheel% for the current pick, then gets
    damped by pack rank: the objectively best cards in the pack won't wheel
    no matter what ALSA says.

    Returns (score_multiplier, wheel_pct, reason).
    """
    if pick_number >= 9:
        return 1.0, 0.0, None  # nothing meaningful wheels from pick 9 on
    alsa = card.alsa
    if alsa is None or alsa <= pick_number:
        return 1.0, 0.0, None
    coeffs = WHEEL_COEFFICIENTS[min(pick_number - 1, len(WHEEL_COEFFICIENTS) - 1)]
    prob = _polyval(coeffs, alsa)
    if rank_in_pack == 0:
        prob *= 0.10   # best card in pack: someone will take it
    elif rank_in_pack <= 2:
        prob *= 0.40
    prob = max(0.0, min(100.0, prob))
    if prob >= 75.0 and rank_in_pack >= 4:
        return 0.8, prob, (f"Likely wheels (~{prob:.0f}%) — you can take "
                           f"something else and still get it back")
    return 1.0, prob, None


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def evaluate_pack(
    pack: Sequence[CardData],
    pool: Sequence[CardData],
    pick_number: int,
    pack_number: int = 1,
    *,
    picks_per_pack: int = DEFAULT_PICKS_PER_PACK,
    fmt: Optional[FormatContext] = None,
    color_signals: Optional[Mapping[str, float]] = None,
) -> list[PickGuidance]:
    """Rank the pack. Pure function — safe to call on every pack update.

    Args:
        pack: normalized cards in the current pack.
        pool: normalized cards already picked, IN PICK ORDER (recency bias
            in lane detection depends on order).
        pick_number: pick within the current pack (1-based).
        pack_number: 1-3.
        picks_per_pack: pack size (MTGA Premier = 14).
        fmt: format context (baselines + pair win rates); defaults applied.
        color_signals: optional accumulated open-lane signals (e.g. summed
            :func:`compute_pack_signals` across previous picks). Used as a
            small tie-breaker toward open colors.

    Returns:
        :class:`PickGuidance` list sorted best-first, scores clamped 0-100.
    """
    if not pack:
        return []
    fmt = fmt or FormatContext()
    pick_number = max(1, min(picks_per_pack + 1, pick_number))
    pack_number = max(1, min(PACKS_PER_DRAFT, pack_number))
    total_picks = picks_per_pack * PACKS_PER_DRAFT
    overall_pick = (pack_number - 1) * picks_per_pack + pick_number

    lane = compute_lane(pool, fmt)
    needs = analyze_pool(pool, fmt, lane)
    lane_pair = lane.pair_key
    main = set(lane.main_colors)
    on_color_pool = sum(
        1 for c in pool if c.colors and all(col in main for col in c.colors)
    ) if main else len(pool)

    # Pack-relative power baseline (bombs are relative to THIS pack).
    pack_wrs = [c.gih_wr_pct for c in pack if c.gih_wr_pct]
    pack_mean = statistics.mean(pack_wrs) if pack_wrs else fmt.mean_gih_wr_pct
    pack_std = (statistics.pstdev(pack_wrs) if len(pack_wrs) > 1
                else fmt.std_gih_wr_pct)
    if pack_std <= 0:
        pack_std = fmt.std_gih_wr_pct
    # Floor the pack std at 1pp: in a flat pack (all cards within a point of
    # each other) a tiny raw edge should not z-score as a "bomb". (Guard we
    # added on top of the unrealities logic, which only handles std == 0.)
    pack_std = max(pack_std, 1.0)

    # Rank within pack by raw WR (for wheel damping).
    by_wr = sorted(pack, key=lambda c: c.gih_wr_pct or 0.0, reverse=True)
    pack_rank = {c.grp_id: i for i, c in enumerate(by_wr)}

    signals = dict(color_signals or {})

    results: list[PickGuidance] = []
    for card in pack:
        reasons: list[str] = []

        # Basic lands are never the pick (unless they're all that's left).
        if card.is_basic_land:
            reasons = (["Only card left in the pack"] if len(pack) == 1
                       else ["Basic land — skip"])
            results.append(PickGuidance(
                grp_id=card.grp_id, name=card.name, score=0.0, tier="WEAK",
                reasons=reasons, wheel_probability=0.0,
                gih_wr_pct=card.gih_wr_pct, lane=lane_pair,
            ))
            continue

        # 1. Bayesian-blended base (global → pair archetype across the draft)
        base, base_reason = _blended_base_score(
            card, lane_pair, overall_pick, total_picks, fmt)
        if base_reason:
            reasons.append(base_reason)

        # 2. Pack-relative bomb detection
        bonus, z, is_true_bomb, bomb_reasons = _bomb_bonus(card, pack_mean, pack_std)
        reasons.extend(bomb_reasons)

        # 3. Open-lane lateness signal (pack 1)
        late_bonus, late_reason = _late_signal_bonus(card, z, pack_number, pick_number)
        bonus += late_bonus
        if late_reason:
            reasons.append(late_reason)

        # 3b. Accumulated cross-pick signal tie-breaker (signals.py port)
        if signals and card.colors:
            strength = sum(signals.get(c, 0.0) for c in card.colors)
            if strength > 10.0:
                base *= 1.05
                open_colors = " and ".join(
                    COLOR_NAMES.get(c, c) for c in card.colors
                    if signals.get(c, 0.0) > 5.0
                ) or "its colors"
                reasons.append(f"{open_colors.capitalize()} keeps coming late "
                               f"— that seat looks open")

        # 4. Lane fit multiplier
        lane_mult, on_lane, lane_reason = _lane_fit(
            card, lane, pack_number, on_color_pool)
        if lane_reason:
            reasons.append(lane_reason)

        # 5. Castability pressure
        cast_mult, cast_reason = _castability(
            card, lane, needs, pack_number, pick_number, z)
        if cast_reason:
            reasons.append(cast_reason)

        # 6. Composition needs
        comp_mult, comp_reason = _composition(
            card, needs, pack_number, len(pool), total_picks)
        if comp_reason:
            reasons.append(comp_reason)

        # 7. Wheel estimate
        wheel_mult, wheel_pct, wheel_reason = _wheel_probability(
            card, pick_number, pack_rank.get(card.grp_id, 99))
        if wheel_reason:
            reasons.append(wheel_reason)

        score = (base + bonus) * lane_mult * cast_mult * comp_mult * wheel_mult
        score = max(0.0, min(100.0, score))

        if not reasons and card.gih_wr_pct is not None:
            # Always give the overlay/voice SOMETHING true to say.
            delta = card.gih_wr_pct - fmt.mean_gih_wr_pct
            if delta >= 1.0:
                reasons.append("Solid card — above the format's average win rate")
            elif delta <= -2.0:
                reasons.append("Below the format's average win rate")
            else:
                reasons.append("Average playable for the format")

        results.append(PickGuidance(
            grp_id=card.grp_id,
            name=card.name,
            score=round(score, 1),
            tier=get_tier(score),
            reasons=reasons,
            wheel_probability=round(wheel_pct, 1),
            gih_wr_pct=card.gih_wr_pct,
            z_score=round(z, 2),
            is_bomb=(z >= BOMB_Z_SCORE and cast_mult > 0.4),
            on_lane=on_lane,
            lane=lane_pair,
        ))

    results.sort(key=lambda g: g.score, reverse=True)
    return results
