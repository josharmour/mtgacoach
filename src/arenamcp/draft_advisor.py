"""Explain draft choices from current card rules and the actual drafted pool."""

from __future__ import annotations

import concurrent.futures
import html
import json
import logging
import re
from collections import Counter
from typing import Any

from arenamcp.draft_guidance import COLOR_LETTERS, FormatContext, analyze_pool, compute_lane, normalize_card
from arenamcp.draft_plan import plan_colors
from arenamcp.limited_rules import rules_profile, synergy_evidence, synergy_graph
from arenamcp.set_primer import normalize_colors

logger = logging.getLogger(__name__)

_PIP_RE = re.compile(r"\{(.*?)\}")

# Proxy call classes (backends/proxy.py CALL_CLASSES) for the metrics lines.
DRAFT_PICK_CALL_CLASS = "draft.pick"
DRAFT_BUILD_CALL_CLASS = "draft.build"


def _call_labels(backend: Any, call_class: str) -> dict[str, Any]:
    """``call_class`` for backends whose complete() accepts it (older/test ones: none).

    Passing an unknown keyword would hit the TypeError fallback below, which
    also drops the budget and the JSON response format.
    """
    try:
        from arenamcp.game_plan import accepted_call_kwargs
    except Exception:  # pragma: no cover - the label is optional
        return {}
    return accepted_call_kwargs(backend, call_class=call_class)


DRAFT_SYSTEM_PROMPT = """You are an expert Limited draft coach. Build a playable, coherent deck
from the cards the player ACTUALLY picked. Card rules in the supplied pack and pool
are authoritative; do not invent abilities or rely on memorized card names.

Choose exactly picks_required cards from this pack. In PickTwo, evaluate the TWO
cards together: their interaction, colors, curve, and combined contribution matter.
Explain each pick's actual function and, where relevant, how it works with a NAMED
card already in the pool. Shared creature types or words alone are not synergy:
identify an enabler, its payoff, the triggering condition, and sufficient support.
Examples: scry/surveil enables a Thopter payoff; artifact tokens enable a Dragon
replacement effect; cheap creatures give counters and combat tricks useful targets.
Do not imply that every card that draws a card enables scry or surveil.
Adding loyalty counters is not activating a loyalty ability. Bounce does not
destroy a creature or trigger its death abilities. Different counter types are
not interchangeable. A hybrid symbol can be paid with either of its colors;
it does not inherently require a splash. Tricks are not early creature plays.
Read related_faces too: prepared spells, Adventures, and alternate faces have
their own costs and conditions. Do not treat a linked spell as freely repeatable.

Early picks should preserve flexibility. As the actual pool develops, settle on
supported main colors and a specific game plan. Continue that plan unless card
quality, real pool support, or mana justify changing it; explain a change.
Lane discipline (how strong drafters commit): picks 1-4 of pack 1 take the best
card and declare NO lane (lane = ""); describe a lean in plan if you like. From pick
5 on, a lane may be declared only when the ACTUAL pool already casts at least two
cards in it and no other pair casts clearly more; a single bomb is a lean, not a
lane. color_signals (0-1 per color) say how strongly good cards of that color have
been arriving late, i.e. what the table is passing: when the pool is close between
pairs, settle toward the colors that are flowing and away from ones that are cut.
previous_lane is the committed lane: keep lane equal to it unless the pool now
casts at least two more cards in another pair (three from pack 2 on, since
abandoning picks costs more as the pool grows; one fewer when that pair is clearly
flowing or its cards are clearly stronger than the held lane's). One strong
off-color card is a speculative pick or a splash, never a new lane; a change must
name the pool cards that support the new pair. A lane the pool does not support is
discarded.
Previous advice is provisional: the user may have picked different cards. Never
count a previous recommendation as owned unless it appears in the actual pool.
Prefer early creatures, interaction, fixing, and missing enablers over more expensive
payoffs or combat tricks when the deck lacks bodies. Track cheap plays, creature
density, removal, mana requirements, and top-end saturation. Avoid a deck of five-mana
creatures and buffs with no early board. Off-color cards need realistic fixing.
Do not spend picks on ordinary basic lands while useful spells remain in the pack.

Draft the DECK, not the best card. In order of importance:
1. Lane: stay castable in the deck we are building (deck_plan, previous_lane).
2. The planned archetype: set_strategy is this set's pre-draft research (archetype
   tiers, plans, engines, payoffs, enablers, synergy notes, traps). deck_plan lists
   the live archetypes for this pool, each with its engine and what the pool already
   holds (creatures, two-drops, removal, card advantage, finishers, engine enablers
   and payoffs). Fill what the deck lacks: a payoff when enablers are plentiful,
   enablers when payoffs wait for them, removal and early creatures when short.
3. Rules-text synergy with cards ALREADY drafted (supported_synergies, card text).
4. Card quality: quality_band (strong / good / average / below average / weak /
   rarely played / unknown) summarizes 17lands results. It is about a quarter of the
   decision: it breaks ties between cards that serve the deck about equally, and a
   true bomb can still be worth a lane change early. "unknown" is not weak.
heuristic_rankings is the deterministic ranking on exactly these terms (rank,
deck_value, quality_band, why); pick from its top cards unless the card rules
show it missed something. Never cite win-rate numbers; none are supplied. If card
rules are missing, say so and avoid invented synergy.

Pick ONLY from pack_choices: every picks[].grp_id must appear in pickable_grp_ids.
already_drafted_not_pickable lists cards we already own; they can never be picked again.
Return ONLY JSON:
{"picks": [{"grp_id": 123, "reason": "1-2 concise sentences explaining why",
"synergy_with": []}], "plan": "one sentence describing the deck's supported theme",
"lane": "the plan's main colors as a WUBRG code such as UR (normally previous_lane), or an empty string while open",
"needs": ["up to three concrete remaining needs"],
"alternative": {"grp_id": 789, "reason": "why it loses to the chosen pick(s)"}}
lane must be exactly the main colors the plan names (an archetype or guild such as
Izzet is UR); a splash or a color merely kept open is never part of lane.
synergy_with may contain only pool_grp_id values of already drafted cards (or the other chosen card)
that supported_synergies links to this pick; when supported_synergies is empty,
synergy_with must be []. Unsupported ids are discarded. Only assert named synergies
present in supported_synergies; it lists conservative rules-text enabler/payoff
links, not guaranteed trigger frequency. Explain the
actual condition and costs. For unmodeled interactions, explain card roles instead
of claiming a verified synergy. Loyalty counters never substitute for +1/+1 counters.
An alternative is optional. Keep each explanation under 40 words.
"""


def _text(value: Any, words: int = 40) -> str:
    return " ".join(value.split()[:words]) if isinstance(value, str) else ""


def _plan_lane(plan: str, stated: Any, archetypes: list[dict] | None) -> dict[str, str]:
    """The lane the plan text names, overriding the model's ``lane`` when they disagree.

    2026-10-08 FRA: the plan said Izzet from P1p5 on while the spoken lane said
    blue-black, so the lane is recomputed from the plan; the model's own code
    counts only for a plan that names no colors.
    """
    stated = normalize_colors(stated) if isinstance(stated, str) and len(stated.strip()) <= 2 else ""
    parsed = plan_colors(plan, archetypes)
    if parsed.main and stated and stated != parsed.main:
        logger.warning(
            "Draft lane %s disagrees with the plan's colors %s (%s); using the plan",
            stated,
            parsed.main,
            plan,
        )
    return {"lane": parsed.main or stated, "splash": parsed.splash}


# 2026-10-10 FRA quick draft: the spoken lane went BG, RG, BG, UG, RG, UB, BG, BG
# over picks 3-10 because the model re-derived it from each pack's best card
# (P1p7 took a blue card while declaring red-green). Later that day a lane was
# declared red-green on pick 4 from one green card and held for four picks of
# blue cards. The rules below follow what strong drafters do (see the
# 2026-10-10 research in docs/DECISIONS.md): best card and no lane for the
# first picks, a lane only once the pool supports it, and a switch only when
# the pool clearly favours the other pair, with the bar rising in later packs.
LANE_OPEN_PICKS = 4  # pack 1 picks 1-4: best card, no lane declared
LANE_MIN_SUPPORT = 2  # a declared lane must already cast this many pool cards
LANE_SWITCH_MARGIN = 2  # pack 1: a switch needs this many more castable cards
LANE_SWITCH_MARGIN_LATE = 3  # pack 2 on: abandoning picks costs more

ALL_PAIRS = ("WU", "WB", "WR", "WG", "UB", "UR", "UG", "BR", "BG", "RG")


def _castable(card: Any, lane: str) -> bool:
    """Whether every colored pip of ``card`` can be paid from ``lane`` (hybrid: either half)."""
    pips = _PIP_RE.findall((card.mana_cost or "").split(" // ", 1)[0])
    colored = [{part.upper() for part in pip.split("/") if part.upper() in COLOR_LETTERS} for pip in pips]
    colored = [pip for pip in colored if pip]
    if not colored:
        return all(color in lane for color in card.colors)
    return all(pip & set(lane) for pip in colored)


def lane_support(pool: list[dict[str, Any]], lane: str) -> int:
    """How many colored nonland pool cards ``lane`` (one or two colors) casts outright."""
    if not 1 <= len(lane) <= 2:
        return 0
    count = 0
    for raw in pool:
        card = normalize_card(raw)
        if card.is_land or not card.colors:
            continue
        count += _castable(card, lane)
    return count


def best_support(pool: list[dict[str, Any]], width: int) -> tuple[str, int]:
    """(lane, support) of the pair (width 2) or single color (width 1) the pool casts most of."""
    lanes = ALL_PAIRS if width == 2 else tuple(COLOR_LETTERS)
    scored = [(lane_support(pool, lane), lane) for lane in lanes]
    support, lane = max(scored, default=(0, ""))
    return (lane, support) if support else ("", 0)


_FIXING_RE = re.compile(r"add (?:[^.]*?)(?:one mana of any color|any color|\{(?:[WUBRG])\})", re.IGNORECASE)


def splash_supported(pool: list[dict[str, Any]], color: str) -> bool:
    """Whether the pool holds a source for ``color`` outside basics: a land or artifact that adds it or any color."""
    if not color:
        return False
    for raw in pool:
        text = html.unescape(re.sub(r"<[^>]+>", "", str(raw.get("oracle_text") or "")))
        for match in _FIXING_RE.finditer(text):
            clause = match.group(0).lower()
            if "any color" in clause or f"{{{color.lower()}}}" in clause:
                return True
    return False


def validate_splash(pool: list[dict[str, Any]], lane: str, splash: str) -> tuple[str, str]:
    """(the splash to keep, why dropped): a splash needs fixing in the pool; a lane color is never a splash."""
    kept = "".join(c for c in normalize_colors(splash) if c not in lane) if splash else ""
    if not kept:
        return "", ""
    supported = "".join(c for c in kept if splash_supported(pool, c))
    dropped = "".join(c for c in kept if c not in supported)
    why = f"no fixing in the pool for a {dropped} splash" if dropped else ""
    return supported, why


# A pair is "flowing" when its colors' accumulated signal is this strong and
# this far above the alternative's.
SIGNAL_FLOWING = 0.5
SIGNAL_EDGE = 0.25
# Quality (mean GIH win rate of the castable cards) this far apart changes the
# switch margin by one: value above replacement, not raw card count.
QUALITY_EDGE = 0.02


def lane_signal(signals: dict[str, float] | None, lane: str) -> float:
    """Mean accumulated openness of a lane's colors (0 when unknown)."""
    if not signals or not lane:
        return 0.0
    return sum(float(signals.get(color, 0.0)) for color in lane) / len(lane)


def lane_quality(pool: list[dict[str, Any]], lane: str) -> float | None:
    """Mean GIH win rate of the colored pool cards the lane casts (None without ratings)."""
    rates = []
    for raw in pool:
        card = normalize_card(raw)
        if card.is_land or not card.colors or not _castable(card, lane):
            continue
        rate = raw.get("gih_wr")
        if isinstance(rate, (int, float)):
            rates.append(float(rate) / (100.0 if rate > 1 else 1.0))
    return sum(rates) / len(rates) if rates else None


def _flowing(signals: dict[str, float] | None, lane: str, against: str) -> bool:
    mine, theirs = lane_signal(signals, lane), lane_signal(signals, against)
    return mine >= SIGNAL_FLOWING and mine >= theirs + SIGNAL_EDGE


def _signal_note(signals: dict[str, float] | None, lane: str) -> str:
    return f"{lane} signal {lane_signal(signals, lane):.2f}" if signals else ""


def validate_lane(
    previous: str,
    proposed: str,
    pool: list[dict[str, Any]],
    *,
    pack_number: int = 1,
    pick_number: int = 1,
    signals: dict[str, float] | None = None,
) -> tuple[str, str]:
    """(the lane to use, why) given the committed lane, the model's proposal, the pool and the table's signals.

    - Pack 1 picks 1-4 with nothing committed: no lane, whatever is proposed.
    - Declaring from open: the proposed lane must cast at least
      ``LANE_MIN_SUPPORT`` pool cards and be within one card of the best lane
      of its width (a pair or a single main color); two short is enough when
      the proposed colors are clearly flowing, and a lane within one card is
      refused when a better-supported lane is clearly flowing instead.
    - Settling the second color (U -> UB) uses the same tests.
    - Switching a committed lane needs ``LANE_SWITCH_MARGIN`` more castable
      cards than the held lane (``LANE_SWITCH_MARGIN_LATE`` from pack 2 on),
      one fewer when the new pair is flowing or its cards are clearly
      stronger, one more when the held lane's cards are clearly stronger.
      Proposing fewer colors than the committed lane, or none, never
      un-commits it.
    """
    previous = normalize_colors(previous) if previous else ""
    proposed = normalize_colors(proposed) if proposed and len(proposed) <= 2 else ""
    if proposed == previous:
        return previous, ""

    def declare(label: str) -> tuple[str, str] | None:
        support = lane_support(pool, proposed)
        best_lane, best = best_support(pool, len(proposed))
        signal = _signal_note(signals, proposed)
        if support < LANE_MIN_SUPPORT:
            return None, f"{proposed} casts {support} pool card(s); best {best_lane or 'none'} casts {best}"
        if support >= best - 1:
            if (
                best_lane
                and best_lane != proposed
                and best >= support
                and _flowing(signals, best_lane, proposed)
            ):
                return None, (
                    f"{best_lane} casts {best} and is flowing ({_signal_note(signals, best_lane)}) "
                    f"vs {proposed} ({support}, {signal})"
                )
            return proposed, f"{label}: the pool casts {support} cards in {proposed}" + (
                f" ({signal})" if signal else ""
            )
        if support >= best - 2 and _flowing(signals, proposed, best_lane):
            return (
                proposed,
                f"{label} on the signal: {proposed} is flowing ({signal}), casts {support} vs best {best}",
            )
        return None, f"{proposed} casts {support} pool card(s); best {best_lane or 'none'} casts {best}"

    if not previous:
        if not proposed:
            return "", ""
        if pack_number <= 1 and pick_number <= LANE_OPEN_PICKS:
            return "", f"picks 1-{LANE_OPEN_PICKS} take the best card; no lane yet"
        lane, why = declare("declared")
        return (lane, why) if lane else ("", f"stay open: {why}")
    if len(previous) == 1 and len(proposed) == 2 and previous in proposed:
        lane, why = declare("second color settled")
        return (lane, why) if lane else (previous, f"held at {previous}: {why}")
    if len(proposed) < len(previous):
        return previous, f"held at {previous}: a committed lane is not dropped for '{proposed or 'open'}'"
    margin = LANE_SWITCH_MARGIN if pack_number <= 1 else LANE_SWITCH_MARGIN_LATE
    notes = []
    if _flowing(signals, proposed, previous):
        margin -= 1
        notes.append(f"{proposed} is flowing")
    held_q, new_q = lane_quality(pool, previous), lane_quality(pool, proposed)
    if held_q is not None and new_q is not None:
        if new_q >= held_q + QUALITY_EDGE:
            margin -= 1
            notes.append(f"{proposed} cards stronger ({new_q:.3f} vs {held_q:.3f})")
        elif held_q >= new_q + QUALITY_EDGE:
            margin += 1
            notes.append(f"{previous} cards stronger ({held_q:.3f} vs {new_q:.3f})")
    margin = max(1, margin)
    mine, theirs = lane_support(pool, previous), lane_support(pool, proposed)
    extra = f"; {', '.join(notes)}" if notes else ""
    if theirs >= mine + margin:
        return proposed, f"switch: the pool casts {theirs} cards in {proposed} vs {mine} in {previous}{extra}"
    return previous, f"held at {previous}: {proposed} casts {theirs} vs {mine} (needs +{margin}{extra})"


def _band_from_gih(gih: Any) -> str:
    """A coarse 17lands band when the caller supplied a raw GIH win rate (no set baseline)."""
    if not isinstance(gih, (int, float)):
        return "unknown"
    for floor, word in ((0.60, "strong"), (0.57, "good"), (0.54, "average"), (0.52, "below average")):
        if gih >= floor:
            return word
    return "weak"


def _card_details(card: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_card(card)
    oracle = html.unescape(re.sub(r"<[^>]+>", "", str(card.get("oracle_text") or "")))
    lines = list(dict.fromkeys(line.strip() for line in oracle.splitlines() if line.strip()))
    return {
        "grp_id": card["grp_id"],
        "name": card.get("name", "Unknown"),
        "mana_cost": normalized.mana_cost,
        "mana_value": normalized.cmc,
        "colors": list(normalized.colors),
        "type_line": card.get("type_line", ""),
        "oracle_text": "\n".join(lines),
        "quality_band": card.get("quality_band") or _band_from_gih(card.get("gih_wr")),
        "power": card.get("power", ""),
        "toughness": card.get("toughness", ""),
        "related_faces": card.get("related_faces") or [],
        "rules_roles": rules_profile(card),
    }


def _answer_summary(payload: Any, cards: list[dict[str, Any]]) -> str:
    """A rejected answer in card names, so later reviews can judge the model, not just the error."""
    if not isinstance(payload, dict):
        return _text(json.dumps(payload, ensure_ascii=False, default=str), 60)
    names = {card["grp_id"]: card.get("name", "Unknown") for card in cards}

    def name(grp_id: Any) -> Any:
        return names.get(grp_id, grp_id) if isinstance(grp_id, int) else grp_id

    picks = payload.get("picks") if isinstance(payload.get("picks"), list) else []
    summary = [
        {
            "pick": name(pick.get("grp_id")),
            "synergy_with": [name(g) for g in pick.get("synergy_with") or []]
            if isinstance(pick.get("synergy_with"), list)
            else pick.get("synergy_with"),
            "reason": _text(pick.get("reason"), 40),
        }
        for pick in picks
        if isinstance(pick, dict)
    ]
    return json.dumps(
        {"picks": summary, "plan": _text(payload.get("plan"), 30)}, ensure_ascii=False, default=str
    )


_WIN_RATE_NOTE = re.compile(r"(?:GIH|WR|win rate)[^;|,]*\d|\d+(?:\.\d+)?%|\bz [+-]?\d", re.IGNORECASE)


def _without_win_rates(row: Any) -> Any:
    """A ranking row for the prompt with any win-rate figures left out."""
    if not isinstance(row, dict):
        return row
    clean = dict(row)
    for key in ("reason", "why"):
        if isinstance(clean.get(key), str):
            parts = re.split(r"\s*[;|]\s*", clean[key])
            clean[key] = "; ".join(part for part in parts if part and not _WIN_RATE_NOTE.search(part))
    for key in ("gih_wr", "win_rate", "score_breakdown"):
        clean.pop(key, None)
    return clean


def pool_summary(cards: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = [normalize_card(card) for card in cards]
    lane = compute_lane(normalized, FormatContext())
    needs = analyze_pool(normalized, FormatContext(), lane)
    return {
        "main_colors": list(lane.main_colors[:2]),
        "creatures": needs.creatures,
        "early_plays": needs.early_plays,
        "removal": needs.removal,
        "five_plus_mana": needs.heavy_drops,
        "fixing": needs.fixing,
        "pool_size": len(cards),
    }


class DraftAdvisor:
    """One bounded model call per pick, with a validated deterministic fallback."""

    def __init__(self, backend: Any, timeout: float = 8.0) -> None:
        self._backend = backend
        self._timeout = timeout
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._pending: concurrent.futures.Future | None = None
        self._event_name = ""
        self._pool_size = 0
        self._plan = ""
        self._lane = ""  # the validated pair the current plan commits to, "" while open
        self._splash = ""

    def _complete(
        self,
        message: str,
        system_prompt: str = DRAFT_SYSTEM_PROMPT,
        max_tokens: int = 1200,
        response_format: dict | None = None,
        call_class: str = DRAFT_PICK_CALL_CLASS,
    ) -> str:
        try:
            kwargs: dict[str, Any] = {"temperature": 0.0, "request_timeout_s": self._timeout}
            if response_format:
                kwargs["response_format"] = response_format
            kwargs.update(_call_labels(self._backend, call_class))
            return self._backend.complete(system_prompt, message, max_tokens, **kwargs)
        except TypeError:
            return self._backend.complete(system_prompt, message)

    # 2026-10-05 sealed: 2400 tokens let glm-5.3-flash spend 1284 on reasoning
    # and truncate the JSON (finish_reason=length), so the model review never ran.
    DECK_MAX_TOKENS = 8000

    def recommend_deck(self, fallback: dict[str, Any]) -> dict[str, Any]:
        """Model review of the counted build: compares the scored candidates, validated, one retry."""
        from arenamcp.limited_deck import DECK_SYSTEM_PROMPT, validate_deck

        pool = fallback.get("pool_cards") or []
        if not pool or (self._pending is not None and not self._pending.done()):
            return fallback
        counts = Counter(card["grp_id"] for card in pool)
        unique = {card["grp_id"]: card for card in pool}
        message = json.dumps(
            {
                "target_size": 40,
                "previous_plan": self._plan,
                "supported_synergies": synergy_graph(pool),
                "set_strategy": fallback.get("set_strategy") or {},
                "deck_plan": fallback.get("deck_plan") or {},
                "scored_candidates": fallback.get("candidates") or [],
                "pool": [{**_card_details(card), "count": counts[grp_id]} for grp_id, card in unique.items()],
            },
            ensure_ascii=False,
        )
        error: Exception | None = None
        for _attempt in range(2):
            prompt = message
            if error is not None:
                prompt += f"\n\nYour previous answer was rejected ({error}). Return ONLY valid JSON for a legal deck."
            self._pending = self._executor.submit(
                self._complete,
                prompt,
                DECK_SYSTEM_PROMPT,
                self.DECK_MAX_TOKENS,
                {"type": "json_object"},
                DRAFT_BUILD_CALL_CLASS,
            )
            try:
                response = self._pending.result(timeout=self._timeout)
                start = response.find("{")
                if start < 0:
                    raise ValueError("Deck response has no JSON object")
                payload, _end = json.JSONDecoder().raw_decode(response[start:])
                result = validate_deck(payload, pool, source="card_rules")
                return {**fallback, **result}
            except concurrent.futures.TimeoutError as exc:
                error = exc
                break
            except Exception as exc:
                error = exc
        logger.warning("Deck reasoning unavailable; keeping counted fallback build: %s", error)
        return fallback

    def recommend(self, pack: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        cards = pack.get("cards") or []
        pool = pack.get("picked_cards") or []
        if not cards:
            return fallback
        event_name = str(pack.get("event_name") or pack.get("set_code") or "")
        if event_name != self._event_name or len(pool) < self._pool_size:
            self._plan = ""
            self._lane = ""
            self._splash = ""
        self._event_name = event_name
        self._pool_size = len(pool)
        if self._pending is not None and not self._pending.done():
            return fallback
        required = min(max(1, int(pack.get("picks_per_pack") or 1)), len(cards))
        # 2026-10-06 P1p8-9: the pool came first with the same shape as the
        # pack, and the model "picked" Geist of Saint Thalia from its own pool
        # twice. Pack choices now come first with their ids listed; pool cards
        # carry pool_grp_id so no pool id can pass for a pickable one.
        counted_pool: dict[int, dict[str, Any]] = {}
        for card in pool:
            grp_id = card["grp_id"]
            if grp_id not in counted_pool:
                details = _card_details(card)
                details["pool_grp_id"] = details.pop("grp_id")
                counted_pool[grp_id] = {**details, "copies": 0}
            counted_pool[grp_id]["copies"] += 1
        graph = synergy_graph(cards + pool)
        request: dict[str, Any] = {
            "event": event_name,
            "pack_number": pack.get("pack_number"),
            "pick_number": pack.get("pick_number"),
            "picks_required": required,
            "pickable_grp_ids": [card["grp_id"] for card in cards],
            "pack_choices": [_card_details(card) for card in cards],
            "supported_synergies": graph,
        }
        if not graph:
            # 2026-10-06 FRA draft: the graph was empty at every pick, yet the
            # model kept naming partners and 18 of 22 answers were discarded.
            request["synergy_rule"] = "supported_synergies is empty: every synergy_with must be []"
        request.update(
            {
                "previous_plan": self._plan,
                "previous_lane": self._lane,
                "color_signals": pack.get("color_signals") or {},
                "already_drafted_not_pickable": list(counted_pool.values()),
                "pool_summary": pool_summary(pool),
                "heuristic_rankings": [_without_win_rates(row) for row in fallback.get("evaluations") or []],
                "set_strategy": pack.get("set_strategy") or {},
                "deck_plan": pack.get("deck_plan") or [],
            }
        )
        message = json.dumps(request, ensure_ascii=False)
        self._pending = self._executor.submit(
            self._complete, message, DRAFT_SYSTEM_PROMPT, 1200, {"type": "json_object"}, DRAFT_PICK_CALL_CLASS
        )
        response, payload = "", None
        try:
            response = self._pending.result(timeout=self._timeout)
            start = response.find("{")
            if start < 0:
                raise ValueError("Draft response has no JSON object")
            payload, _end = json.JSONDecoder().raw_decode(response[start:])
            result = self._validate(
                payload, cards, pool, required, (pack.get("set_strategy") or {}).get("archetypes")
            )
        except Exception as exc:
            logger.warning("Draft reasoning unavailable; using card-text guidance: %s", exc)
            if payload is not None:
                logger.warning("Rejected draft answer: %s", _answer_summary(payload, cards + pool))
            elif isinstance(response, str) and response:
                logger.warning("Unparsed draft answer: %.300s", response)
            return {**fallback, "reasoning_source": "heuristic"}
        proposed = str(result.get("lane") or "")
        lane, why = validate_lane(
            self._lane,
            proposed,
            pool,
            pack_number=int(pack.get("pack_number") or 1),
            pick_number=int(pack.get("pick_number") or 1),
            signals=pack.get("color_signals") or None,
        )
        if lane != proposed:
            logger.info("Draft lane %s: the model proposed %s (%s)", why, proposed or "open", result["plan"])
            result["lane_rejected"] = proposed
            result["rejected_plan"] = result["plan"]
            result["lane"] = lane
            if lane == self._lane and self._lane:
                # The committed plan is what the next pick continues from.
                result.update({"splash": self._splash, "plan": self._plan})
            else:
                result["splash"] = ""
        elif lane != self._lane:
            logger.info("Draft lane %s (%s)", why or f"{self._lane or 'open'} -> {lane}", result["plan"])
        splash, why = validate_splash(pool, lane, str(result.get("splash") or ""))
        if why:
            logger.info("Draft splash dropped: %s (%s)", why, result["plan"])
        result["splash"] = splash if lane else ""
        self._plan = result["plan"]
        self._lane = str(result.get("lane") or "")
        self._splash = str(result.get("splash") or "")
        advice = f"Pack {pack.get('pack_number')}, Pick {pack.get('pick_number')}. "
        advice += " ".join(f"Take {pick['name']}. {pick['reason']}" for pick in result["recommendations"])
        advice += f" Plan: {result['plan']}"
        detailed = advice
        if result["needs"]:
            detailed += "\nStill need: " + "; ".join(result["needs"])
        if result.get("alternative"):
            alternative = result["alternative"]
            detailed += f"\nAlternative: {alternative['name']}. {alternative['reason']}"
        return {
            **fallback,
            **result,
            "spoken_advice": advice,
            "detailed_advice": detailed,
            "reasoning_source": "card_rules",
        }

    @staticmethod
    def _validate(
        payload: Any,
        cards: list[dict],
        pool: list[dict],
        required: int,
        archetypes: list[dict] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Draft response must be an object")
        picks = payload.get("picks")
        if not isinstance(picks, list) or len(picks) != required:
            raise ValueError("Draft response has the wrong number of picks")
        available = Counter(card["grp_id"] for card in cards)
        names = {card["grp_id"]: card["name"] for card in cards}
        selected = []
        for pick in picks:
            if not isinstance(pick, dict) or type(pick.get("grp_id")) is not int:
                raise ValueError("Draft pick must identify a pack card")
            grp_id = pick["grp_id"]
            if available[grp_id] <= 0:
                raise ValueError("Draft pick is absent from the pack")
            available[grp_id] -= 1
            reason = _text(pick.get("reason"))
            if not reason:
                raise ValueError("Draft pick needs an explanation")
            selected.append({"grp_id": grp_id, "name": names[grp_id], "reason": reason})
        owned = {card["grp_id"] for card in pool} | {pick["grp_id"] for pick in selected}
        by_id = {card["grp_id"]: card for card in cards + pool}
        for pick, raw in zip(selected, picks, strict=True):
            # synergy_with explains a pick; it does not make it. A partner that
            # is outside the pool or lacks a rules-text link is dropped and
            # logged, and the pick is still judged on everything else.
            claimed = raw.get("synergy_with") or []
            if not isinstance(claimed, list):
                claimed = [claimed]
            kept: list[int] = []
            dropped: list[Any] = []
            evidence: list[dict] = []
            for grp_id in claimed:
                edges = (
                    synergy_evidence(by_id[pick["grp_id"]], by_id[grp_id])
                    if type(grp_id) is int and grp_id in owned and grp_id != pick["grp_id"]
                    else []
                )
                if edges and grp_id not in kept:
                    kept.append(grp_id)
                    evidence.extend(edges)
                elif not edges:
                    dropped.append(grp_id)
            if dropped:
                partners = [
                    by_id[grp_id].get("name", grp_id) if type(grp_id) is int and grp_id in by_id else grp_id
                    for grp_id in dropped
                ]
                logger.warning(
                    "Draft synergy claim stripped: %s with %s has no rules-text link in the pool; reason was: %s",
                    pick["name"],
                    partners,
                    pick["reason"],
                )
                pick["unsupported_synergy"] = partners
                pick["reason"] = f"{pick['reason']} (synergy unverified)"
            pick["synergy_with"] = kept
            pick["synergy_evidence"] = evidence
        plan = _text(payload.get("plan"))
        if not plan:
            raise ValueError("Draft response needs a deck plan")
        needs = payload.get("needs") or []
        if not isinstance(needs, list):
            raise ValueError("Draft needs must be a list")
        result = {"recommendations": selected, "plan": plan, "needs": [_text(need, 16) for need in needs[:3]]}
        result.update(_plan_lane(plan, payload.get("lane"), archetypes))
        alternative = payload.get("alternative")
        if isinstance(alternative, dict):
            grp_id = alternative.get("grp_id")
            reason = _text(alternative.get("reason"))
            if type(grp_id) is int and available[grp_id] > 0 and reason:
                result["alternative"] = {"grp_id": grp_id, "name": names[grp_id], "reason": reason}
        return result
