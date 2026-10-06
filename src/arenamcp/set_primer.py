"""Pre-draft set strategy: 17lands performance data plus card rules, built once per set.

A drafter who opens P1p1 cold ranks cards one pack at a time. A prepared one
already knows the set's archetypes, which commons carry them, which payoffs
need which enablers, and which colors actually win. This module builds that
preparation before the first pick and caches it per set:

1. Data (always): every rated card's 17lands games-in-hand win rate, improvement
   when drawn, average pick (ATA) and last-seen (ALSA), plus two-color archetype
   win rates. Per-card baselines are z-scores within the set.
2. Synthesis (when the model answers): one bounded model pass reads the card
   rules and the data and names the archetypes, their payoffs, enablers and key
   commons, synergy clusters, pick principles and traps. Every card it names is
   validated against the set; unknown names are dropped.

If the synthesis fails, the data-only primer still drives picks.
"""

from __future__ import annotations

import json
import logging
import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PRIMER_DIR = Path.home() / ".arenamcp" / "cache" / "set_primers"
PRIMER_VERSION = 1
PRIMER_MAX_AGE_S = 7 * 24 * 3600
MIN_RATED_GAMES = 200
COLOR_ORDER = "WUBRG"
TWO_COLOR_PAIRS = ("WU", "WB", "WR", "WG", "UB", "UR", "UG", "BR", "BG", "RG")
PAIR_NAMES = {
    "WU": "Azorius",
    "WB": "Orzhov",
    "WR": "Boros",
    "WG": "Selesnya",
    "UB": "Dimir",
    "UR": "Izzet",
    "UG": "Simic",
    "BR": "Rakdos",
    "BG": "Golgari",
    "RG": "Gruul",
}
ROLE_KEYS = ("payoffs", "enablers", "key_commons", "key_uncommons")

_ANALYST = """You are a professional Magic: The Gathering Limited analyst preparing the draft
strategy a strong player reads BEFORE drafting this set. You get card rules text and 17lands
performance data (GIH = games-in-hand win rate; IWD = improvement when drawn; ATA = average
pick taken, lower = drafted earlier; ALSA = average last seen). Data shows what wins; rules
explain why. Prefer what the data supports over reputation. Name real interactions: an
enabler, the payoff it feeds, and the condition. Shared words or creature types alone are not
synergy. Use ONLY card names exactly as given. Return ONLY one JSON object, no prose."""

ARCHETYPE_PROMPT = (
    _ANALYST
    + """
Analyze ONE two-color archetype using only the cards listed (its colors plus colorless).
Return: {"name": "short archetype name", "plan": "one or two sentences: how it wins",
 "tier": 1, "payoffs": ["card"], "enablers": ["card"], "key_commons": ["card"],
 "key_uncommons": ["card"], "avoid": ["card"],
 "synergy_notes": [{"cards": ["card", "card"], "why": "the concrete interaction"}]}
tier: 1 strong, 2 average, 3 weak (use the archetype win rate). Up to 8 names per list,
up to 6 synergy_notes."""
)

FORMAT_PROMPT = (
    _ANALYST
    + """
Summarize the FORMAT as a whole. Return: {"overview": "2-3 sentences",
 "speed": "fast|medium|slow", "mechanics": [{"name": "...", "summary": "how it plays"}],
 "pick_principles": ["short actionable principle"],
 "traps": [{"card": "card", "why": "looks strong but underperforms, or needs missing support"}]}
Give 5-8 pick_principles and 4-8 traps grounded in the data."""
)


@dataclass
class SetCard:
    grp_id: int
    name: str
    colors: str
    rarity: str
    types: str
    cmc: float | None = None
    oracle: str = ""
    gih_wr: float | None = None
    iwd: float | None = None
    alsa: float | None = None
    ata: float | None = None
    oh_wr: float | None = None
    games: int = 0
    baseline: float | None = None
    # 17lands game win rate and play rate (share of drafted copies that made a
    # deck). Present even when GIH is suppressed for a small sample, so they
    # tell a rarely played card from a strong rare that is simply scarce.
    game_wr: float | None = None
    play_rate: float | None = None


@dataclass
class SetPrimer:
    set_code: str
    version: int = PRIMER_VERSION
    built_at: float = 0.0
    source: str = "data"  # "model" once a validated synthesis is merged
    overview: str = ""
    speed: str = ""
    mechanics: list[dict] = field(default_factory=list)
    archetypes: list[dict] = field(default_factory=list)
    synergy_notes: list[dict] = field(default_factory=list)
    pick_principles: list[str] = field(default_factory=list)
    traps: list[dict] = field(default_factory=list)
    pair_stats: dict[str, dict] = field(default_factory=dict)
    cards: dict[int, SetCard] = field(default_factory=dict)

    # -- serialization ---------------------------------------------------

    def to_json(self) -> str:
        data = asdict(self)
        data["cards"] = {str(grp_id): asdict(card) for grp_id, card in self.cards.items()}
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> SetPrimer:
        data = json.loads(text)
        cards = {int(grp_id): SetCard(**card) for grp_id, card in (data.pop("cards", {}) or {}).items()}
        return cls(**data, cards=cards)

    # -- queries ----------------------------------------------------------

    def card(self, grp_id: int) -> SetCard | None:
        return self.cards.get(int(grp_id))

    def by_name(self) -> dict[str, SetCard]:
        return {card.name.lower(): card for card in self.cards.values()}

    def archetype(self, colors: str) -> dict | None:
        key = normalize_colors(colors)
        return next((a for a in self.archetypes if a.get("colors") == key), None)

    def card_roles(self, name: str) -> list[tuple[str, str]]:
        """(archetype colors, role) for every archetype list that names this card."""
        lowered = name.lower()
        roles = []
        for archetype in self.archetypes:
            for role in ROLE_KEYS:
                if any(lowered == entry.lower() for entry in archetype.get(role) or []):
                    roles.append((archetype.get("colors", ""), role))
        return roles

    def is_trap(self, name: str) -> bool:
        lowered = name.lower()
        return any(lowered == str(trap.get("card", "")).lower() for trap in self.traps)

    def prompt_context(self, lane: str = "", max_archetypes: int = 10) -> dict[str, Any]:
        """Compact strategy for pick and deck prompts, the lane's archetypes first."""
        lane_key = normalize_colors(lane)
        ranked = sorted(
            self.archetypes,
            key=lambda a: (
                0 if lane_key and set(a.get("colors", "")) <= set(lane_key) | set("C") else 1,
                a.get("tier", 2),
                -(self.pair_stats.get(a.get("colors", ""), {}).get("win_rate") or 0),
            ),
        )
        return {
            "set": self.set_code,
            "overview": self.overview,
            "speed": self.speed,
            "pick_principles": self.pick_principles[:8],
            "archetypes": [
                {
                    **{key: archetype.get(key) for key in ("colors", "name", "plan", "tier")},
                    "win_rate": self.pair_stats.get(archetype.get("colors", ""), {}).get("win_rate"),
                    **{role: (archetype.get(role) or [])[:6] for role in ROLE_KEYS},
                }
                for archetype in ranked[:max_archetypes]
            ],
            "synergy_notes": self.synergy_notes[:12],
            "traps": [trap.get("card") for trap in self.traps[:10]],
        }


def mana_value(cost: str | None) -> float | None:
    """Mana value from Arena ("o2oUoU") or Scryfall ("{2}{U}{U}") cost strings."""
    import re

    text = str(cost or "")
    if not text:
        return None
    symbols = re.findall(r"\{([^}]*)\}", text) if "{" in text else [s for s in text.split("o") if s]
    total = 0
    for symbol in symbols:
        symbol = symbol.strip().upper()
        if symbol.isdigit():
            total += int(symbol)
        elif symbol and symbol not in {"X", "Y", "Z"}:
            total += 1
    return float(total)


def arena_rules_lookup(database: Any) -> Callable[[int, str], dict[str, Any] | None]:
    """Rules text and mana value from the local Arena card database (no download)."""

    def lookup(grp_id: int, _name: str) -> dict[str, Any] | None:
        card = database.get_card(grp_id) if database is not None else None
        if card is None:
            return None
        return {
            "oracle_text": card.oracle_text,
            "cmc": mana_value(card.mana_cost),
            "type_line": card.type_line,
        }

    return lookup


def normalize_colors(colors: str | None) -> str:
    letters = {c for c in str(colors or "").upper() if c in COLOR_ORDER}
    return "".join(c for c in COLOR_ORDER if c in letters)


# ---------------------------------------------------------------------------
# Data primer
# ---------------------------------------------------------------------------


def collect_set_cards(
    raw_ratings: list[dict[str, Any]],
    rules_lookup: Callable[[int, str], dict[str, Any] | None] | None = None,
) -> dict[int, SetCard]:
    """17lands card rows (one per card, with mtga_id) joined to card rules."""
    cards: dict[int, SetCard] = {}
    for row in raw_ratings:
        grp_id, name = row.get("mtga_id"), str(row.get("name") or "").strip()
        if not isinstance(grp_id, int) or grp_id <= 0 or not name:
            continue
        rules = (rules_lookup(grp_id, name) if rules_lookup else None) or {}
        cards[grp_id] = SetCard(
            grp_id=grp_id,
            name=name,
            colors=normalize_colors(row.get("color")),
            rarity=str(row.get("rarity") or "")[:1].upper(),
            types=" ".join(row.get("types") or [])
            if isinstance(row.get("types"), list)
            else str(row.get("types") or ""),
            cmc=rules.get("cmc"),
            oracle=str(rules.get("oracle_text") or ""),
            gih_wr=row.get("ever_drawn_win_rate"),
            iwd=row.get("drawn_improvement_win_rate"),
            alsa=row.get("avg_seen"),
            ata=row.get("avg_pick"),
            oh_wr=row.get("opening_hand_win_rate"),
            games=int(row.get("ever_drawn_game_count") or 0),
            game_wr=row.get("win_rate"),
            play_rate=row.get("play_rate"),
        )
    rated = [c.gih_wr for c in cards.values() if c.gih_wr is not None and c.games >= MIN_RATED_GAMES]
    if len(rated) >= 10:
        mean, spread = statistics.fmean(rated), statistics.pstdev(rated) or 1.0
        for card in cards.values():
            if card.gih_wr is not None and card.games >= MIN_RATED_GAMES:
                card.baseline = round((card.gih_wr - mean) / spread, 3)
    return cards


def pair_table(color_stats: dict[str, Any]) -> dict[str, dict]:
    """Two-color archetype win rates and share of games from 17lands color ratings."""
    rows = {}
    total = sum(getattr(stat, "games", 0) for key, stat in color_stats.items() if key in TWO_COLOR_PAIRS)
    for key in TWO_COLOR_PAIRS:
        stat = color_stats.get(key)
        if stat is None or not getattr(stat, "games", 0):
            continue
        rows[key] = {
            "win_rate": round(stat.win_rate, 4),
            "games": stat.games,
            "share": round(stat.games / total, 4) if total else None,
        }
    return rows


def data_archetypes(cards: dict[int, SetCard], pairs: dict[str, dict]) -> list[dict]:
    """Per pair: the best-performing commons and uncommons that fit its colors."""
    win_rates = [row["win_rate"] for row in pairs.values()]
    archetypes = []
    for colors in TWO_COLOR_PAIRS:
        fitting = [
            card
            for card in cards.values()
            if card.baseline is not None and card.colors and set(card.colors) <= set(colors)
        ]
        best = sorted(fitting, key=lambda card: -(card.baseline or 0))
        signposts = [card.name for card in best if card.colors == colors and card.rarity == "U"]
        tier = 2
        if colors in pairs and len(win_rates) >= 3:
            ordered = sorted(win_rates, reverse=True)
            rate = pairs[colors]["win_rate"]
            tier = (
                1
                if rate >= ordered[len(ordered) // 3]
                else 3
                if rate <= ordered[-(len(ordered) // 3) - 1]
                else 2
            )
        archetypes.append(
            {
                "colors": colors,
                "name": PAIR_NAMES[colors],
                "plan": "",
                "tier": tier,
                "payoffs": signposts[:3],
                "enablers": [],
                "key_commons": [card.name for card in best if card.rarity == "C"][:8],
                "key_uncommons": [card.name for card in best if card.rarity == "U"][:6],
                "avoid": [],
            }
        )
    return archetypes


def data_primer(set_code: str, raw_ratings: list[dict], color_stats: dict, rules_lookup=None) -> SetPrimer:
    cards = collect_set_cards(raw_ratings, rules_lookup)
    pairs = pair_table(color_stats)
    primer = SetPrimer(set_code=set_code.upper(), built_at=time.time(), pair_stats=pairs, cards=cards)
    primer.archetypes = data_archetypes(cards, pairs)
    traps = [
        card
        for card in cards.values()
        if card.rarity in {"R", "M"} and card.baseline is not None and card.baseline < -0.75
    ]
    primer.traps = [
        {"card": card.name, "why": f"rare with GIH {card.gih_wr:.1%}, well below the set average"}
        for card in sorted(traps, key=lambda card: card.baseline or 0)[:8]
    ]
    return primer


# ---------------------------------------------------------------------------
# Model synthesis
# ---------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "?" if value is None else f"{value * 100:.1f}"


def _card_rows(cards: list[SetCard], max_oracle: int = 220) -> list[str]:
    rows = ["name | colors | rarity | type | cmc | GIH | IWD | ATA | ALSA | rules"]
    for card in sorted(cards, key=lambda card: (card.colors or "Z", card.name)):
        oracle = " ".join(card.oracle.split())[:max_oracle]
        iwd = "?" if card.iwd is None else f"{card.iwd * 100:+.1f}"
        ata = "?" if card.ata is None else f"{card.ata:.1f}"
        alsa = "?" if card.alsa is None else f"{card.alsa:.1f}"
        cmc = "?" if card.cmc is None else f"{card.cmc:g}"
        rows.append(
            f"{card.name} | {card.colors or 'C'} | {card.rarity} | {card.types} | {cmc} | "
            f"{_pct(card.gih_wr)} | {iwd} | {ata} | {alsa} | {oracle}"
        )
    return rows


def _pair_line(primer: SetPrimer) -> str:
    pairs = sorted(primer.pair_stats.items(), key=lambda item: -item[1]["win_rate"])
    return ", ".join(f"{PAIR_NAMES[k]} {k} {_pct(v['win_rate'])}% ({v['games']} games)" for k, v in pairs)


def archetype_prompt(primer: SetPrimer, colors: str) -> str:
    cards = [card for card in primer.cards.values() if set(card.colors) <= set(colors)]
    stats = primer.pair_stats.get(colors, {})
    return "\n".join(
        [
            f"SET {primer.set_code}. Archetype {PAIR_NAMES[colors]} ({colors}): win rate "
            f"{_pct(stats.get('win_rate'))}% over {stats.get('games', '?')} games.",
            f"All two-color win rates: {_pair_line(primer)}",
            "",
            *_card_rows(cards),
        ]
    )


def format_prompt(primer: SetPrimer) -> str:
    return "\n".join(
        [
            f"SET {primer.set_code}. Two-color win rates: {_pair_line(primer)}",
            "",
            *_card_rows(list(primer.cards.values()), 140),
        ]
    )


def synthesis_prompt(primer: SetPrimer, max_oracle: int = 220) -> str:
    """Whole-set table (kept for diagnostics and tests)."""
    return "\n".join(
        [
            f"SET {primer.set_code}. Two-color win rates: {_pair_line(primer)}",
            "",
            *_card_rows(list(primer.cards.values()), max_oracle),
        ]
    )


def _name_index(primer: SetPrimer) -> dict[str, str]:
    names = {card.name.lower(): card.name for card in primer.cards.values()}
    for card in primer.cards.values():
        if " // " in card.name:
            names.setdefault(card.name.split(" // ")[0].lower(), card.name)
    return names


def _known(names: dict[str, str], values: Any, limit: int = 8) -> list[str]:
    result = []
    for value in values if isinstance(values, list) else []:
        name = names.get(str(value).strip().lower())
        if name and name not in result:
            result.append(name)
    return result[:limit]


def _clean_archetype(entry: dict, colors: str, names: dict[str, str], primer: SetPrimer) -> dict:
    base = next((a for a in primer.archetypes if a["colors"] == colors), {})
    tier = entry.get("tier")
    return {
        "colors": colors,
        "name": str(entry.get("name") or PAIR_NAMES[colors])[:60],
        "plan": str(entry.get("plan") or "")[:400],
        "tier": tier if tier in (1, 2, 3) else base.get("tier", 2),
        **{role: _known(names, entry.get(role)) or base.get(role, []) for role in ROLE_KEYS},
        "avoid": _known(names, entry.get("avoid")),
        "synergy_notes": [
            {"cards": _known(names, note.get("cards"), 4), "why": str(note.get("why") or "")[:300]}
            for note in entry.get("synergy_notes") or []
            if isinstance(note, dict) and len(_known(names, note.get("cards"), 4)) >= 2
        ][:6],
    }


def _merge_format(primer: SetPrimer, payload: dict, names: dict[str, str]) -> None:
    primer.overview = str(payload.get("overview") or "")[:800]
    speed = str(payload.get("speed") or "").lower()
    primer.speed = speed if speed in {"fast", "medium", "slow"} else ""
    primer.mechanics = [
        {"name": str(m.get("name"))[:60], "summary": str(m.get("summary") or "")[:300]}
        for m in payload.get("mechanics") or []
        if isinstance(m, dict) and m.get("name")
    ][:10]
    primer.pick_principles = [str(p)[:200] for p in payload.get("pick_principles") or [] if str(p).strip()][
        :10
    ]
    traps = [
        {"card": names[str(t.get("card")).lower()], "why": str(t.get("why") or "")[:200]}
        for t in payload.get("traps") or []
        if isinstance(t, dict) and str(t.get("card", "")).lower() in names
    ]
    if traps:
        primer.traps = traps[:12]


def merge_synthesis(primer: SetPrimer, payload: Any) -> SetPrimer:
    """Merge a whole-set synthesis (archetypes + format fields) into the data primer."""
    if not isinstance(payload, dict):
        raise ValueError("Primer synthesis must be an object")
    names = _name_index(primer)
    merged = []
    for entry in payload.get("archetypes") or []:
        if not isinstance(entry, dict):
            continue
        colors = normalize_colors(entry.get("colors"))
        if colors not in TWO_COLOR_PAIRS or any(a["colors"] == colors for a in merged):
            continue
        merged.append(_clean_archetype(entry, colors, names, primer))
    if len(merged) < 3:
        raise ValueError("Primer synthesis named too few valid archetypes")
    by_colors = {a["colors"]: a for a in merged}
    primer.archetypes = [by_colors.get(a["colors"], a) for a in primer.archetypes]
    for entry in merged:
        primer.synergy_notes += entry.pop("synergy_notes", [])
    primer.synergy_notes += [
        {"cards": _known(names, note.get("cards"), 4), "why": str(note.get("why") or "")[:300]}
        for note in payload.get("synergy_notes") or []
        if isinstance(note, dict) and len(_known(names, note.get("cards"), 4)) >= 2
    ]
    primer.synergy_notes = primer.synergy_notes[:40]
    _merge_format(primer, payload, names)
    primer.source = "model"
    return primer


def _ask(backend: Any, system: str, message: str, timeout: float, max_tokens: int = 4000) -> str:
    try:
        return backend.complete(
            system,
            message,
            max_tokens,
            temperature=0.0,
            request_timeout_s=timeout,
            response_format={"type": "json_object"},
        )
    except TypeError:
        return backend.complete(system, message)


def _parse_payload(response: str | None) -> Any:
    start = (response or "").find("{")
    if start < 0:
        raise ValueError("Primer response has no JSON object")
    payload, _end = json.JSONDecoder().raw_decode(response[start:])
    if not isinstance(payload, dict):
        raise ValueError("Primer response must be one JSON object")
    return payload


def _ask_validated(
    backend: Any,
    system: str,
    message: str,
    validate: Callable[[dict], Any],
    timeout: float,
    attempts: int = 2,
) -> Any:
    """One validated answer, retrying once with the failure quoted back."""
    error: Exception | None = None
    for _attempt in range(attempts):
        prompt = (
            message
            if error is None
            else f"{message}\n\nYour previous answer was rejected ({error}). Return ONLY valid JSON."
        )
        try:
            return validate(_parse_payload(_ask(backend, system, prompt, timeout)))
        except Exception as exc:
            error = exc
    raise ValueError(str(error))


def synthesize(primer: SetPrimer, backend: Any, timeout: float = 120.0, workers: int = 3) -> SetPrimer:
    """Per-archetype and format-level model passes, merged into the data primer.

    2026-10-05: one whole-set call returned four of ten archetypes once and
    degenerate JSON the next time, so each archetype is its own small, focused,
    validated call; any piece that fails keeps its data-only version.
    """
    from concurrent.futures import ThreadPoolExecutor

    names = _name_index(primer)

    def archetype_task(colors: str) -> dict | None:
        def validate(payload: dict) -> dict:
            entry = _clean_archetype(payload, colors, names, primer)
            if not any(entry[role] for role in ROLE_KEYS) or not entry["plan"]:
                raise ValueError("archetype answer named no valid cards or plan")
            return entry

        try:
            return _ask_validated(
                backend, ARCHETYPE_PROMPT, archetype_prompt(primer, colors), validate, timeout
            )
        except Exception as exc:
            logger.info("Set primer %s %s archetype synthesis failed: %s", primer.set_code, colors, exc)
            return None

    def format_task() -> dict | None:
        def validate(payload: dict) -> dict:
            if not str(payload.get("overview") or "").strip():
                raise ValueError("format answer has no overview")
            return payload

        try:
            return _ask_validated(backend, FORMAT_PROMPT, format_prompt(primer), validate, timeout)
        except Exception as exc:
            logger.info("Set primer %s format synthesis failed: %s", primer.set_code, exc)
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        format_future = pool.submit(format_task)
        archetype_results = list(pool.map(archetype_task, TWO_COLOR_PAIRS))
        format_payload = format_future.result()

    synthesized = [entry for entry in archetype_results if entry]
    if not synthesized and not format_payload:
        logger.warning("Set primer synthesis for %s unavailable; using data primer", primer.set_code)
        return primer
    by_colors = {entry["colors"]: entry for entry in synthesized}
    primer.archetypes = [by_colors.get(a["colors"], a) for a in primer.archetypes]
    for entry in synthesized:
        primer.synergy_notes += entry.pop("synergy_notes", [])
    if format_payload:
        _merge_format(primer, format_payload, names)
    primer.source = "model"
    logger.info(
        "Set primer %s synthesis: %d/10 archetypes, format %s",
        primer.set_code,
        len(synthesized),
        "ok" if format_payload else "missing",
    )
    return primer


# ---------------------------------------------------------------------------
# Cache and background builder
# ---------------------------------------------------------------------------


class SetPrimerService:
    """Builds each set's primer once in the background and serves it from disk."""

    def __init__(
        self,
        *,
        backend_fn: Callable[[], Any],
        ratings_fn: Callable[[str], list[dict]],
        color_stats_fn: Callable[[str], dict],
        rules_lookup: Callable[[int, str], dict | None] | None = None,
        cache_dir: Path = PRIMER_DIR,
    ) -> None:
        self._backend_fn = backend_fn
        self._ratings_fn = ratings_fn
        self._color_stats_fn = color_stats_fn
        self._rules_lookup = rules_lookup
        self._dir = cache_dir
        self._lock = threading.Lock()
        self._primers: dict[str, SetPrimer] = {}
        self._building: dict[str, threading.Thread] = {}
        self._quick: dict[str, SetPrimer] = {}

    def _path(self, set_code: str) -> Path:
        return self._dir / f"{set_code.upper()}.json"

    def _load(self, set_code: str) -> SetPrimer | None:
        path = self._path(set_code)
        try:
            primer = SetPrimer.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            if path.exists():
                logger.info("Ignoring unreadable set primer %s: %s", path, exc)
            return None
        if primer.version != PRIMER_VERSION or time.time() - primer.built_at > PRIMER_MAX_AGE_S:
            return None
        return primer

    def get(self, set_code: str | None) -> SetPrimer | None:
        if not set_code:
            return None
        key = set_code.upper()
        with self._lock:
            if key not in self._primers:
                primer = self._load(key)
                if primer is not None:
                    self._primers[key] = primer
            return self._primers.get(key)

    def ensure(self, set_code: str | None) -> None:
        """Start building a missing or stale primer; returns immediately."""
        if not set_code or self.get(set_code) is not None:
            return
        key = set_code.upper()
        with self._lock:
            worker = self._building.get(key)
            if worker is not None and worker.is_alive():
                return
            worker = threading.Thread(target=self._build, args=(key,), daemon=True, name=f"primer-{key}")
            self._building[key] = worker
            worker.start()

    def quick(self, set_code: str | None) -> SetPrimer | None:
        """Data-only primer, built synchronously, while the full one is still building."""
        if not set_code:
            return None
        key = set_code.upper()
        with self._lock:
            cached = self._quick.get(key)
        if cached is not None:
            return cached
        try:
            primer = data_primer(key, self._ratings_fn(key), self._color_stats_fn(key), self._rules_lookup)
        except Exception as exc:
            logger.info("Quick set primer for %s unavailable: %s", key, exc)
            return None
        with self._lock:
            self._quick[key] = primer
        return primer

    def build_now(self, set_code: str) -> SetPrimer | None:
        self._build(set_code.upper())
        return self.get(set_code)

    def _build(self, key: str) -> None:
        started = time.monotonic()
        try:
            raw = self._ratings_fn(key)
            primer = data_primer(key, raw, self._color_stats_fn(key), self._rules_lookup)
            if len([c for c in primer.cards.values() if c.baseline is not None]) < 20:
                logger.warning("Set primer for %s skipped: too little 17lands data", key)
                return
            backend = self._backend_fn()
            if backend is not None:
                primer = synthesize(primer, backend)
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path(key).write_text(primer.to_json(), encoding="utf-8")
            with self._lock:
                self._primers[key] = primer
            logger.info(
                "Set primer for %s built (%s, %d cards, %d archetypes) in %.0fs",
                key,
                primer.source,
                len(primer.cards),
                len(primer.archetypes),
                time.monotonic() - started,
            )
        except Exception as exc:
            logger.warning("Set primer for %s failed: %s", key, exc)
