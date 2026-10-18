"""Counted, validated forty-card builds and spoken cuts for Limited pools."""

import html
import logging
import re
from collections import Counter
from itertools import combinations
from types import SimpleNamespace
from typing import Any

from arenamcp.draft_guidance import normalize_card
from arenamcp.limited_rules import rules_profile, synergy_evidence

logger = logging.getLogger(__name__)

BASIC_NAMES = {"W": "Plains", "U": "Islands", "B": "Swamps", "R": "Mountains", "G": "Forests"}
COLOR_NAMES = {"W": "white", "U": "blue", "B": "black", "R": "red", "G": "green"}

DECK_SYSTEM_PROMPT = """Build exactly 40 cards for this Limited deck (draft or sealed) from the ACTUAL
counted pool. Use only available copies. Ordinary basic lands can be added freely through basic_lands;
nonbasic lands and every spell must come from the pool. Aim for 23 spells and 17 total
lands, adjusting within 15-19 lands only when the curve warrants it. Prioritize a coherent
two-color plan, early creatures, interaction, supported enablers/payoffs, and a castable
mana base. Splash a third color only for a few high-impact cards (bombs, premium removal)
with a single pip of that color, never early drops, and with at least three sources of it
(basics, on-color dual lands, or fixing). Sealed pools are deeper and games slower: bombs
and removal matter more than in draft. Read full rules and related_faces, not card names.
Hybrid mana can use either color. Adding loyalty counters is not a loyalty activation.
Honor the previous theme only if this actual pool supports it. Missing ratings are unknown.
When set_strategy is supplied (this set's 17lands data plus card-rules analysis), prefer its
stronger archetypes the pool supports, include its payoffs only with enough enablers, and
avoid its traps unless the pool has nothing better.
deck_plan.drafted_lane is the lane the draft committed to (deck_plan.plan its plan): build
that deck unless this pool clearly supports a stronger one, and say why if you change it.
scored_candidates are complete counted builds per color pair (some with a supported splash),
scored on the archetype's engine (enablers vs payoffs), rules-text links between the chosen
cards, the drafted lane, creature count, cheap plays, removal, bombs, splash consistency and
card quality (a minor part); "archetype" and "engine" name the plan each one plays.
"format" says draft or sealed. Start from the highest scoring candidate; change colors or
cards only for a concrete reason (a bomb, real synergy, missing early plays) and say why in plan.
Use supported_synergies as grounded links, checking the supplied rules' conditions and
costs. For unmodeled interactions, describe roles rather than inventing a verified combo.
Return JSON only: {"main_deck": [{"grp_id":123,"count":2}],
"basic_lands":{"G":9,"U":8}, "plan":"one short sentence",
"cuts":[{"grp_id":456,"reason":"why this card loses its slot, at most 12 words"}]}.
main_deck includes chosen pool spells AND any drafted nonbasic lands. Supply a concrete
cut reason for every nonbasic pool card excluded, including excess copies. Never assume
the pool is already in the editor: this is a proposed build, not a verified editor view.
"""


def _nonbasics(pool: list[dict]) -> list[dict]:
    return [card for card in pool if "basic land" not in card.get("type_line", "").lower()]


def _produces(card: dict) -> set[str]:
    """Colors a land or fixing card can make (Arena writes mana as {oG}, Scryfall as {G})."""
    colors = {color for color in (card.get("produced_mana") or []) if color in "WUBRG"}
    text = html.unescape(re.sub(r"<[^>]*>", "", card.get("oracle_text", ""))).lower()
    for clause in re.findall(r"\badd\b[^.\n]*", text):
        colors.update(symbol.upper() for symbol in re.findall(r"\{o?([wubrg])\}", clause))
        if "any color" in clause or "chosen color" in clause:
            colors.update("WUBRG")
    if re.search(r"search your library for (?:a|up to \w+) basic land", text):
        colors.update("WUBRG")
    return colors


def _castable(card: dict, colors: tuple[str, ...]) -> bool:
    for symbol in re.findall(r"\{([^}]+)\}", card.get("mana_cost", "")):
        choices = set(symbol.split("/"))
        if choices & set("WUBRGC") and not choices & set(colors) and not choices & {"2", "P"}:
            return False
    return True


def _name_key(name: str) -> str:
    return " ".join(name.lower().replace("’", "'").split())


def _entry_grp_id(entry: Any, by_name: dict[str, int], what: str) -> int:
    """The pool grp_id an entry names: its integer ``grp_id``, or its ``name`` looked up in the pool.

    Every rejection says exactly what was wrong, because the message is fed
    back to the model for its one retry. 2026-10-09 sealed: both attempts died
    with a bare KeyError ``'grp_id'`` (entries carried names only), so the
    retry prompt said "rejected ('grp_id')" and the review never ran.
    """
    if not isinstance(entry, dict):
        raise ValueError(f'Each {what} entry must be an object like {{"grp_id": 123, ...}}')
    # 2026-10-10 14:56 sealed: a cut came back keyed " grp_id" (leading space)
    # and the whole deck review was discarded over the stray whitespace.
    entry = {str(key).strip(): value for key, value in entry.items()}
    grp_id = entry.get("grp_id")
    if grp_id is None and isinstance(entry.get("name"), str):
        grp_id = by_name.get(_name_key(entry["name"]))
        if grp_id is None:
            raise ValueError(f"{what} entry names a card not in the pool: {entry['name']!r}")
    if type(grp_id) is not int:
        raise ValueError(f"Each {what} entry needs the integer grp_id of a pool card (got {entry!r})")
    return grp_id


CUT_REASON_DEFAULT = "not in the final 40"


def validate_deck(payload: dict, pool: list[dict], *, source: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Deck response must be a JSON object")
    cards = _nonbasics(pool)
    available = Counter(card["grp_id"] for card in cards)
    by_id = {card["grp_id"]: card for card in cards}
    by_name = {_name_key(str(card.get("name") or "")): card["grp_id"] for card in cards}
    kept: Counter = Counter()
    main_deck = payload.get("main_deck")
    if not isinstance(main_deck, list):
        raise ValueError("Deck response needs a main_deck list")
    for entry in main_deck:
        grp_id = _entry_grp_id(entry, by_name, "main_deck")
        count = entry.get("count", 1)
        if type(count) is not int or count <= 0:
            raise ValueError("Deck entries need integer IDs and positive counts")
        kept[grp_id] += count
        if kept[grp_id] > available[grp_id]:
            raise ValueError("Deck uses unavailable cards or too many copies")
    basics = payload.get("basic_lands")
    if not isinstance(basics, dict) or any(
        color not in BASIC_NAMES or type(count) is not int or count < 0 for color, count in basics.items()
    ):
        raise ValueError("Invalid basic land allocation")
    mana_colors = {color for color, count in basics.items() if count}
    for grp_id in kept:
        card = by_id[grp_id]
        if "land" in card.get("type_line", "").lower():
            mana_colors.update(_produces(card))
    uncastable = [
        by_id[grp_id]["name"] for grp_id in kept if not _castable(by_id[grp_id], tuple(mana_colors))
    ]
    if uncastable:
        # Named so a model retry can fix the exact cards (2026-10-05 sealed review).
        raise ValueError(
            f"Deck includes colors its proposed mana base ({''.join(sorted(mana_colors)) or 'none'}) "
            f"cannot produce: {', '.join(uncastable[:6])}"
        )
    total = sum(kept.values()) + sum(basics.values())
    lands = sum(
        count for grp_id, count in kept.items() if "land" in by_id[grp_id].get("type_line", "").lower()
    )
    lands += sum(basics.values())
    if total != 40 or not 15 <= lands <= 19:
        raise ValueError("Draft deck must total 40 cards with a supported land count")
    cut_entries = payload.get("cuts") or []
    if not isinstance(cut_entries, list):
        raise ValueError("cuts must be a list")
    reasons = {
        _entry_grp_id(entry, by_name, "cuts"): {str(k).strip(): v for k, v in entry.items()}.get("reason", "")
        for entry in cut_entries
    }
    cuts = []
    missing_reasons = 0
    for grp_id, count in (available - kept).items():
        reason = reasons.get(grp_id)
        if not isinstance(reason, str) or not reason.strip():
            # 2026-10-11 Collector Sealed: an 84-card pool leaves 40+ cuts and the
            # model skipped a few notes, so both attempts died on "Every cut needs
            # an explanation" and the review of the deck itself was thrown away.
            # The cut set is derived from the deck, not from these notes.
            missing_reasons += 1
            reason = CUT_REASON_DEFAULT
        cuts.append(
            {
                "grp_id": grp_id,
                "name": by_id[grp_id]["name"],
                "count": count,
                "reason": " ".join(reason.split()[:16]),
            }
        )
    if missing_reasons:
        logger.info("Deck review left %d cut(s) unexplained; using the default note", missing_reasons)
    plan = payload.get("plan")
    if not isinstance(plan, str) or not plan.strip():
        raise ValueError("Deck needs a plan")
    main = [
        {"grp_id": grp_id, "name": by_id[grp_id]["name"], "count": count} for grp_id, count in kept.items()
    ]
    land_parts = [f"{count} {BASIC_NAMES[color]}" for color, count in basics.items() if count]
    land_parts.extend(
        f"{entry['count']} {entry['name']}"
        for entry in main
        if "land" in by_id[entry["grp_id"]].get("type_line", "").lower()
    )
    spoken = "For a 40-card deck from your drafted pool, leave these out of the main deck. "
    if cuts:
        spoken += " ".join(f"Cut {cut['count']} {cut['name']}: {cut['reason']}." for cut in cuts)
    else:
        spoken = "Keep all your drafted spells in this 40-card build."
    spoken += f" Use {total - lands} spells and {lands} lands: {', '.join(land_parts)}. That's 40 cards."
    detailed = spoken + "\nPlan: " + plan.strip()
    detailed += "\nKeep:\n" + "\n".join(f"{entry['count']} {entry['name']}" for entry in main)
    return {
        "main_deck": main,
        "basic_lands": dict(basics),
        "cuts": cuts,
        "total_cards": total,
        "land_count": lands,
        "spell_count": total - lands,
        "plan": plan.strip(),
        "spoken_advice": spoken,
        "detailed_text": detailed,
        "reasoning_source": source,
        "basis": "drafted_pool",
    }


RARITY_PRIOR = {"mythic": 6.0, "rare": 4.0, "uncommon": 1.0}
BOMB_GIH = 61.0


def _gih(card: dict) -> float | None:
    return normalize_card(card).gih_wr_pct


def _card_value(card: dict, quality_scale: float = 1.0, cost: float | None = None) -> float:
    """Card quality in GIH points above 50; unrated rares/mythics get a prior, not zero.

    2026-10-05 sealed: an unrated mythic (Craterclaw Colossus) scored 0 and was
    never considered, while ordinary commons with data outranked it.
    ``quality_scale`` < 1 when a DeckStrategy carries the deck's synergy.
    ``cost`` replaces the printed mana value for the curve terms: what the card
    costs in the deck's colors (``_effective_cmc``), less any ramp paying for it.
    """
    normalized = normalize_card(card)
    if normalized.gih_wr_pct is not None:
        value = (normalized.gih_wr_pct - 50) * quality_scale
    else:
        value = RARITY_PRIOR.get(str(card.get("rarity") or "").lower(), 0.0)
    value += 4 if rules_profile(card)["unconditional_body"] else 0
    value += 4 if _builder_removal(card) else 0
    value += 2 if "card_advantage" in normalized.tags else 0
    cmc = normalized.cmc if cost is None else cost
    value += 2 if cmc <= 3 else -max(0, cmc - 4)
    return value


def _rules_text(card: dict) -> str:
    text = html.unescape(re.sub(r"<[^>]*>", "", card.get("oracle_text", ""))).lower().replace("’", "'")
    return re.sub(r"\([^)]*\)", "", text)


# "This spell costs {X} less to cast, where X is ..." / "{1} less for each ...": what
# the reduction usually comes to by the turn the card is cast.
VARIABLE_COST_REDUCTION = 3
_FIXED_REDUCTION = re.compile(r"\bthis spell costs \{o?(\d+)\} less to cast\b(?! for each)")
_VARIABLE_REDUCTION = re.compile(r"\bthis spell costs \{o?(?:x|1)\} less to cast\b")


def _effective_cmc(card: dict, colors: tuple[str, ...] = ()) -> float:
    """Mana a card really costs in a deck of ``colors``.

    Off-color "twobrid" symbols ({2/W}) cost two generic each, and a printed cost
    reduction lowers it. 2026-10-18 pro WU draft: Karn, Gilded Guardian
    ({2/W}{2/U}{2/B}{2/R}{2/G}) read as a 5-drop and took the slot of the pro's
    Mind Meanderer; in WU it costs eight.
    """
    cmc = normalize_card(card).cmc
    symbols = _symbols(card)
    if any("2" in options and len(options) > 1 for options in symbols):
        cmc = 0.0
        for options in symbols:
            single = next(iter(options)) if len(options) == 1 else ""
            if single.isdigit():
                cmc += int(single)
            elif single == "X":
                continue
            elif "2" in options:
                cmc += 1 if options & set(colors) else 2
            else:
                cmc += 1
    text = _rules_text(card)
    fixed = _FIXED_REDUCTION.search(text)
    if fixed:
        cmc -= int(fixed.group(1))
    elif _VARIABLE_REDUCTION.search(text):
        cmc -= VARIABLE_COST_REDUCTION
    return max(0.0, cmc)


_TAP_FOR_MANA = re.compile(r"\{o?t\}:[^\n]*?\badd\b")
_UNTAP_LANDS = re.compile(r"\buntap (?:up to )?(?:two|three|four|\d+) target lands\b|\buntap all lands you control\b")
_LAND_TO_BATTLEFIELD = re.compile(r"\bsearch your library for [^.]*\bland cards?\b[^.]*\bonto the battlefield\b")
_RESTRICTED_MANA = re.compile(r"\bthis mana can't be spent to cast spells from your hand\b|\bspend this mana only\b")


def _ramp(card: dict) -> float:
    """How much a nonland card accelerates the mana: 1 for a mana source, 0.5 if restricted.

    2026-10-18 pro GW draft: Heartwood Crafter ("can't be spent to cast spells
    from your hand", so only exile casts and abilities), Loot, Garruk's untap and
    Emrakul's own exile-to-ramp ability carried him to Emrakul, Ghalta and Ruric
    Thar. A cast trigger that untaps lands ("when you cast this spell") pays for
    nothing else, so only an untap the card can do on its own counts.
    """
    if "land" in card.get("type_line", "").lower():
        return 0.0
    text = _rules_text(card)
    lines = [line for line in text.split("\n") if not line.strip().startswith("when you cast this spell")]
    text = "\n".join(lines)
    if not (_TAP_FOR_MANA.search(text) or _UNTAP_LANDS.search(text) or _LAND_TO_BATTLEFIELD.search(text)):
        return 0.0
    return 0.5 if _RESTRICTED_MANA.search(text) else 1.0


# Ramp wants something to ramp into: a deck with N ramp pieces supports about N
# (up to 3) big threats, and ramp pays part of their cost. 2026-10-18 pro GW draft
# (4-1): 3 ramp pieces + Garruk, then Emrakul, Ghalta and Ruric Thar won most games,
# where our build ran a twelfth two-drop and a third pump spell instead.
TOP_END_CMC = 6
MAX_RAMPED_TOP_END = 3
RAMPED_TOP_END_POINTS = 4.0
MIN_RAMP_FOR_TOP_END = 2.0
RAMP_POINTS = 2.0


def _top_end(card: dict, colors: tuple[str, ...] = ()) -> bool:
    kind = card.get("type_line", "").lower()
    return ("creature" in kind or "planeswalker" in kind) and _effective_cmc(card, colors) >= TOP_END_CMC


def _ramped_slots(chosen, colors: tuple[str, ...] = ()) -> tuple[float, int]:
    """(ramp in these cards, how many big threats that ramp supports)."""
    ramp = sum(_ramp(card) for card in chosen)
    if ramp < MIN_RAMP_FOR_TOP_END:
        return ramp, 0
    return ramp, min(MAX_RAMPED_TOP_END, int(ramp))


def _deck_cost(card: dict, colors: tuple[str, ...], ramp: float, supported: bool) -> float:
    """The curve cost a build charges: real cost in these colors, less ramp for a supported threat."""
    cost = _effective_cmc(card, colors)
    if supported:
        cost = max(TOP_END_CMC - 2, cost - min(ramp, MAX_RAMPED_TOP_END))
    return cost


class DeckStrategy:
    """The draft's strategy for the build: the set playbook and the lane we drafted toward.

    2026-10-13: picks follow the deck (archetype engine, rules-text links, lane)
    with 17Lands about a quarter; the build now uses the same knowledge. Card
    quality still counts, scaled by QUALITY_SCALE, and a card 17Lands rates
    as unplayable gets little synergy credit. ``plan_lane`` (the advisor's
    committed lane, saved with the draft) earns PLAN_BONUS for its colors.
    """

    def __init__(self, primer: Any, plan_lane: str = "", plan: str = "") -> None:
        from arenamcp.draft_autopick import _set_stats
        from arenamcp.draft_playbook import playbook_for

        self.primer = primer
        self.book = playbook_for(primer)
        self.plan_lane = "".join(c for c in "WUBRG" if c in str(plan_lane or "").upper())
        self.plan = plan
        self.stats = _set_stats(primer)
        self._by_name = {card.name.lower(): grp_id for grp_id, card in primer.cards.items()}

    def _grp(self, card: dict) -> int | None:
        grp_id = card.get("grp_id")
        if grp_id is not None and self.primer.card(grp_id) is not None:
            return int(grp_id)
        return self._by_name.get(str(card.get("name") or "").lower())

    def tags(self, card: dict):
        grp_id = self._grp(card)
        return self.book.tag(grp_id) if self.book is not None and grp_id is not None else None

    def z(self, card: dict) -> float | None:
        from arenamcp.draft_autopick import card_value

        grp_id = self._grp(card)
        known = self.primer.card(grp_id) if grp_id is not None else None
        return card_value(known, self.stats)[0] if known is not None else None

    def playable(self, card: dict) -> float:
        from arenamcp.draft_autopick import PLAYABLE_Z, UNPLAYABLE_CREDIT, UNPLAYABLE_Z

        z = self.z(card)
        if z is None or z >= PLAYABLE_Z:
            return 1.0
        return max(UNPLAYABLE_CREDIT, 1.0 - (PLAYABLE_Z - z) / (PLAYABLE_Z - UNPLAYABLE_Z))

    def archetype(self, colors: tuple[str, ...]):
        main = "".join(c for c in "WUBRG" if c in colors)
        return self.book.archetype(main) if self.book is not None and len(main) == 2 else None

    def fit_points(self, card: dict, colors: tuple[str, ...]) -> float:
        plan = self.archetype(colors)
        grp_id = self._grp(card)
        if plan is None or grp_id is None:
            return 0.0
        return ARCHETYPE_POINTS * self.book.fit(grp_id, plan, roles=False)[0] * self.playable(card)

    def link_points(self, card: dict, chosen) -> float:
        """Rules-text links to the cards already chosen, weighted like the pick ranking."""
        from arenamcp.draft_playbook import BROAD_MECHANICS, family

        total = 0.0
        for other in chosen:
            best = 0.0
            for edge in synergy_evidence(card, other):
                strength = BROAD_MECHANICS.get(family(edge["mechanic"]), (0, 1.0))[1]
                if edge.get("indirect"):
                    strength *= 0.5
                payoff = card if edge["payoff_id"] == card.get("grp_id") else other
                tags = self.tags(payoff)
                if tags is not None:
                    strength *= tags.payoff_strength
                best = max(best, strength)
            total += best
        return min(LINK_POINTS_CAP, LINK_POINTS * total) * self.playable(card)

    def engine_points(self, card: dict, colors: tuple[str, ...], chosen) -> float:
        """A payoff wants enablers already in the deck; an enabler wants something to feed."""
        from arenamcp.draft_playbook import BROAD_MECHANICS

        plan, tags = self.archetype(colors), self.tags(card)
        if plan is None or tags is None or tags.land:
            return 0.0
        held = [self.tags(other) for other in chosen]
        held = [t for t in held if t is not None]
        best = 0.0
        for engine in plan.engine:
            enablers = sum(engine in t.engine_enables() for t in held) + 0.5 * sum(
                engine in t.engine_indirect() for t in held
            )
            payoffs = sum(engine in t.engine_payoffs() for t in held)
            if engine in tags.engine_payoffs():
                value = PAYOFF_POINTS * tags.payoff_strength * min(1.0, enablers / 4.0)
            elif engine in tags.engine_enables() or engine in tags.engine_indirect():
                factor = BROAD_MECHANICS.get(engine, (0, 1.0))[1]
                room = 1.0 if enablers < 3 * max(1, payoffs) + 2 else 0.3
                value = ENABLER_POINTS * factor * min(1.0, payoffs) * room
            else:
                continue
            best = max(best, value)
        return best * self.playable(card)

    def coherence(self, chosen, colors: tuple[str, ...]) -> tuple[float, dict]:
        """Whole-deck engine balance for the archetype these colors play, and the drafted lane."""
        plan = self.archetype(colors)
        if plan is None:
            return 0.0, {}
        held = [t for t in (self.tags(card) for card in chosen) if t is not None]
        points, engines = 0.0, {}
        for engine in plan.engine:
            enablers = sum(engine in t.engine_enables() for t in held)
            payoffs = sum(engine in t.engine_payoffs() for t in held)
            engines[engine] = {"enablers": enablers, "payoffs": payoffs}
            points += COHERENCE_POINTS * min(payoffs, enablers / 3.0)
            points -= ORPHAN_PAYOFF_POINTS * max(0.0, payoffs - enablers / 2.0)
        main = "".join(c for c in "WUBRG" if c in colors)
        if self.plan_lane and main == self.plan_lane:
            points += PLAN_BONUS
        return points, {"archetype": plan.name, "engine": engines, "drafted_lane": main == self.plan_lane}


# Strategy terms in the builder's GIH-point units (a card's value is its GIH
# above 50 plus role bonuses of 2-4): archetype fit up to ~4.5, engine balance,
# weighted rules-text links, and the drafted lane's bonus. Card quality keeps
# its full weight in the build (QUALITY_SCALE 1.0): the build picks the best 23
# of cards we already own, and at 0.75 the cheap-body bonuses let 47-50% cards
# replace Verdant Kraken (58.7%) and Icy Reception (2026-10-13 live build).
QUALITY_SCALE = 1.0
ARCHETYPE_POINTS = 6.0
PAYOFF_POINTS = 4.0
ENABLER_POINTS = 2.5
LINK_POINTS = 1.5
LINK_POINTS_CAP = 6.0
COHERENCE_POINTS = 1.5
ORPHAN_PAYOFF_POINTS = 2.0
PLAN_BONUS = 8.0


def _is_legendary(card: dict) -> bool:
    return "legendary" in card.get("type_line", "").lower()


# Sealed games run longer and pools are deeper: bombs and removal carry more,
# a missing two-drop matters less, and a slightly larger splash is acceptable.
FORMAT_WEIGHTS = {
    "draft": {"bomb": 4, "removal_cap": 6, "cheap_penalty": 2, "max_splash": 2},
    "sealed": {"bomb": 6, "removal_cap": 8, "cheap_penalty": 1, "max_splash": 3},
}
SPLASH_CARD_COST = 2.5  # consistency lost per splashed card
SPLASH_BASICS_ONLY_COST = 3.0  # splashing without a dual land or fixing spell
# Consistency lost by adding a third color at all, in the score's GIH-point units.
# 17Lands games (tools/deck_benchmark, 2026-10-06): a splash color costs ~0.12 logit at
# equal card quality, about 11 GIH points spread over 23 spells; with 0 the builder
# splashed one card in ~80% of sealed pools (humans ~40-55%) and scored below its own
# unsplashed builds. 8 (+2.5 per card) was best on older sets and held up on newer ones.
SPLASH_BASE_COST = 8.0
SPLASH_MIN_GIH = BOMB_GIH - 1  # a splash card must be about bomb quality...
SPLASH_REMOVAL_MIN_GIH = 57.0  # ...or premium removal


def _symbols(card: dict) -> list[set[str]]:
    return [set(symbol.split("/")) for symbol in re.findall(r"\{([^}]+)\}", card.get("mana_cost", ""))]


def _splash_pips(card: dict, base: tuple[str, ...], color: str) -> int:
    """Symbols that only the splash color can pay."""
    return sum(color in options and not options & (set(base) | {"2", "P"}) for options in _symbols(card))


def _splash_worthy(card: dict) -> bool:
    """Only bombs and premium removal justify weakening the mana."""
    gih = _gih(card)
    removal = _interaction_kind(card) == "removal"
    if gih is not None:
        return gih >= SPLASH_MIN_GIH or (removal and gih >= SPLASH_REMOVAL_MIN_GIH)
    return str(card.get("rarity") or "").lower() in {"rare", "mythic"} and (
        removal or "evasion" in normalize_card(card).tags
    )


def _required_colors(card: dict) -> set[str]:
    return {
        next(iter(options)) for options in _symbols(card) if len(options) == 1 and options <= set("WUBRG")
    }


def _color_roles(chosen: list[dict]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(main colors, splash colors) from what the deck's cards actually require."""
    needed = Counter(color for card in chosen for color in _required_colors(card))
    ranked = [color for color, _count in needed.most_common()]
    main = tuple(sorted(ranked[:2], key="WUBRG".index))
    return main, tuple(sorted(ranked[2:], key="WUBRG".index))


def _build_for_colors(
    spells: list[dict], colors: tuple[str, ...], preset: tuple = (), strategy: DeckStrategy | None = None
) -> list[dict]:
    candidates = [
        card for card in spells if card.get("type_line") and _castable(card, colors) and card not in preset
    ]
    chosen: list[dict] = list(preset)
    # Ramp earns its slot when the pool has a big threat to ramp into.
    ramp_targets = any(_top_end(card, colors) for card in candidates + chosen)
    while candidates and len(chosen) < 23:
        bodies = sum(rules_profile(card)["unconditional_body"] for card in chosen)
        early = sum(
            rules_profile(card)["unconditional_body"] and normalize_card(card).cmc <= 3 for card in chosen
        )
        expensive = sum(_effective_cmc(card, colors) >= 5 for card in chosen)
        legends = Counter(card["name"] for card in chosen if _is_legendary(card))
        ramp, slots = _ramped_slots(chosen, colors)
        top_end = sum(_top_end(card, colors) for card in chosen)

        def fit(card, bodies=bodies, early=early, expensive=expensive, chosen=tuple(chosen), legends=legends):
            profile = rules_profile(card)
            supported = _top_end(card, colors) and top_end < slots
            cost = _deck_cost(card, colors, ramp, supported)
            value = _card_value(card, QUALITY_SCALE if strategy is not None else 1.0, cost)
            if profile["unconditional_body"]:
                value += 5 if bodies < 14 else 0
                value += 4 if early < 6 and cost <= 3 else 0
            if supported:
                value += RAMPED_TOP_END_POINTS
            elif cost >= 5 and expensive >= 4:
                value -= 6
            if ramp_targets:
                value += RAMP_POINTS * _ramp(card)
            if legends[card["name"]]:
                value -= 10  # a second copy of a legendary card is dead while the first is in play
            if strategy is not None:
                value += strategy.fit_points(card, colors)
                value += strategy.engine_points(card, colors, chosen)
                value += strategy.link_points(card, chosen)
            else:
                value += min(4, sum(bool(synergy_evidence(card, other)) for other in chosen))
            return value

        picked = max(candidates, key=fit)
        candidates.remove(picked)
        chosen.append(picked)
    return chosen


def deck_quality(
    chosen: list[dict],
    colors: tuple[str, ...] = (),
    pair_win_rates: dict | None = None,
    *,
    fmt: str = "draft",
    splash: tuple[str, ...] = (),
    fixing: int = 0,
    strategy: DeckStrategy | None = None,
) -> dict:
    """Whole-deck evaluation: card quality, bombs, removal, bodies, curve, archetype data, splash cost.

    With a ``strategy``: card quality is scaled down, and the archetype fit of
    every card, the engine's enabler/payoff balance and the drafted lane count.
    """
    weights = FORMAT_WEIGHTS.get(fmt, FORMAT_WEIGHTS["draft"])
    ramp, slots = _ramped_slots(chosen, colors)
    supported = set()
    for index, card in sorted(
        enumerate(chosen), key=lambda item: _card_value(item[1]), reverse=True
    ):  # the best big threats are the ones the ramp is for
        if len(supported) < slots and _top_end(card, colors):
            supported.add(index)
    values = [
        _card_value(
            card,
            QUALITY_SCALE if strategy is not None else 1.0,
            _deck_cost(card, colors, ramp, index in supported),
        )
        + (RAMPED_TOP_END_POINTS if index in supported else 0.0)
        for index, card in enumerate(chosen)
    ]
    gihs = [g for g in (_gih(card) for card in chosen) if g is not None]
    creatures = sum(rules_profile(card)["unconditional_body"] for card in chosen)
    removal = sum(_builder_removal(card) for card in chosen)
    cheap = sum(normalize_card(card).cmc <= 2 for card in chosen)
    expensive = sum(_effective_cmc(card, colors) >= 6 for card in chosen) - len(supported)
    bombs = [card["name"] for card in chosen if (_gih(card) or 0) >= BOMB_GIH]
    support = sum(
        bool(synergy_evidence(first, second))
        for index, first in enumerate(chosen)
        for second in chosen[index + 1 :]
    )
    score = (
        sum(values)
        + weights["bomb"] * len(bombs)
        + 2 * min(removal, weights["removal_cap"])
        + min(12, support)
    )
    score -= 3 * max(0, 14 - creatures) + weights["cheap_penalty"] * max(0, 4 - cheap)
    score -= 3 * max(0, expensive - 4)
    splashed = [card["name"] for card in chosen if _required_colors(card) & set(splash)]
    if splashed:
        score -= (
            SPLASH_BASE_COST + SPLASH_CARD_COST * len(splashed) + (0 if fixing else SPLASH_BASICS_ONLY_COST)
        )
    key = "".join(color for color in "WUBRG" if color in colors and color not in splash)
    if pair_win_rates and key in pair_win_rates:
        rates = list(pair_win_rates.values())
        score += 50 * (pair_win_rates[key] - sum(rates) / len(rates))
    plan_info: dict = {}
    if strategy is not None:
        main = tuple(key)
        score += sum(strategy.fit_points(card, main) for card in chosen)
        coherence, plan_info = strategy.coherence(chosen, main)
        score += coherence
    return {
        **plan_info,
        "colors": key,
        "score": round(score, 2),
        "avg_gih": round(sum(gihs) / len(gihs), 1) if gihs else None,
        "creatures": creatures,
        "removal": removal,
        "cheap_plays": cheap,
        "bombs": bombs,
        "synergy_links": support,
        "spells": len(chosen),
        "splash": "".join(splash),
        "splash_cards": splashed,
        "format": fmt,
    }


def candidate_decks(
    pool: list[dict],
    pair_win_rates: dict | None = None,
    top: int = 3,
    *,
    fmt: str = "draft",
    strategy: DeckStrategy | None = None,
) -> list[dict]:
    """The best few two-color builds, plus supported splashes, each scored as a whole deck."""
    weights = FORMAT_WEIGHTS.get(fmt, FORMAT_WEIGHTS["draft"])
    nonbasics = _nonbasics(pool)
    spells = [card for card in nonbasics if "land" not in card.get("type_line", "").lower()]
    lands = [card for card in nonbasics if "land" in card.get("type_line", "").lower()]
    ranked = []
    for colors in combinations("WUBRG", 2):
        base = _build_for_colors(spells, colors, strategy=strategy)
        if len(base) < 21:
            continue
        ranked.append(
            {
                "colors": colors,
                "splash": (),
                "chosen": base,
                "quality": deck_quality(base, colors, pair_win_rates, fmt=fmt, strategy=strategy),
            }
        )
        weakest = min(_card_value(card) for card in base)
        for color in "WUBRG":
            if color in colors:
                continue
            splashable = sorted(
                (
                    card
                    for card in spells
                    if not _castable(card, colors)
                    and _castable(card, colors + (color,))
                    and _splash_pips(card, colors, color) == 1
                    and normalize_card(card).cmc >= 2
                    and _splash_worthy(card)
                    and _card_value(card) >= weakest + 3
                ),
                key=_card_value,
                reverse=True,
            )
            unique = list({card["name"]: card for card in splashable}.values())[: weights["max_splash"]]
            fixing = sum(color in _produces(card) for card in lands) + sum(
                color in _produces(card) and _castable(card, colors) for card in spells
            )
            if not unique or (
                fmt == "draft" and not fixing and not any((_gih(c) or 0) >= BOMB_GIH for c in unique)
            ):
                continue
            chosen = _build_for_colors(spells, colors, preset=tuple(unique), strategy=strategy)
            if len(chosen) < 21:
                continue
            ranked.append(
                {
                    "colors": colors + (color,),
                    "splash": (color,),
                    "chosen": chosen,
                    "quality": deck_quality(
                        chosen,
                        colors + (color,),
                        pair_win_rates,
                        fmt=fmt,
                        splash=(color,),
                        fixing=fixing,
                        strategy=strategy,
                    ),
                }
            )
    ranked.sort(key=lambda item: (len(item["chosen"]), item["quality"]["score"]), reverse=True)
    return ranked[:top]


def fallback_deck(
    pool: list[dict],
    pair_win_rates: dict | None = None,
    *,
    fmt: str = "draft",
    strategy: DeckStrategy | None = None,
) -> dict[str, Any]:
    candidates = candidate_decks(pool, pair_win_rates, top=12, fmt=fmt, strategy=strategy)
    if not candidates:
        return {
            "spoken_advice": "I don't yet have enough castable spells to verify a balanced 40-card build. Check that the full draft pool is available.",
            "detailed_text": "No verified 40-card build is available from this pool.",
            "reasoning_source": "heuristic",
            "basis": "drafted_pool",
        }
    # The alternatives worth comparing are a different color pair and, for a
    # splash, the same pair without it; never three near-copies of one deck.
    best = candidates[0]
    pair = tuple(c for c in best["colors"] if c not in best.get("splash", ()))
    others = [c for c in candidates[1:] if set(c["colors"]) - set(c.get("splash", ())) != set(pair)]
    variants = [c for c in candidates[1:] if c not in others]
    ordered = [best] + others[:1] + variants[:1] + others[1:] + variants[1:]
    options = []
    signatures = set()
    for candidate in ordered:
        option = _build_candidate(pool, candidate)
        signature = (
            tuple(sorted((entry["grp_id"], entry["count"]) for entry in option["main_deck"])),
            tuple(sorted((color, count) for color, count in option["basic_lands"].items() if count)),
        )
        if signature not in signatures:
            options.append(option)
            signatures.add(signature)
        if len(options) == 3:
            break
    result = dict(options[0])
    result["deck_options"] = options
    result["candidates"] = [
        {
            **option["quality"],
            "main_deck": option["main_deck"],
            "basic_lands": option["basic_lands"],
            "cards": [entry["name"] for entry in option["main_deck"] for _ in range(entry["count"])],
        }
        for option in options
    ]
    return result


# A land that makes none of the deck's colors earns one basic's slot in a two-color
# deck only when its abilities feed several of the deck's cards. 2026-10-18 pro GW
# draft: Hexhaven Dueling Arena (re-prepares creatures) over the ninth Forest, with
# four "enters prepared" creatures; it also paid for Emrakul's exile ramp.
UTILITY_LAND_MIN_LINKS = 3


def _utility_land(pool: list[dict], chosen: list[dict], colors: tuple[str, ...]) -> dict | None:
    best, best_links = None, 0
    for card in _nonbasics(pool):
        # Colorless only: a land making an off color is a splash land, not a utility land.
        if "land" not in card.get("type_line", "").lower() or _produces(card):
            continue
        links = sum(bool(synergy_evidence(card, other)) for other in chosen)
        if links >= UTILITY_LAND_MIN_LINKS and links > best_links:
            best, best_links = card, links
    return best


def _build_candidate(pool: list[dict], candidate: dict) -> dict:
    """Allocate lands (on-color duals and fixing first) and validate every option."""
    colors, chosen = tuple(candidate["colors"]), candidate["chosen"]
    splash = tuple(candidate.get("splash") or ())
    main = tuple(color for color in colors if color not in splash)
    land_slots = 40 - len(chosen)
    # A nonbasic land earns a slot by making two of our colors or a splash color;
    # a land that only makes one main color is no better than its basic.
    useful = [
        card
        for card in _nonbasics(pool)
        if "land" in card.get("type_line", "").lower()
        and (len(_produces(card) & set(colors)) >= 2 or _produces(card) & set(splash))
    ]
    nonbasic = useful[:4]
    utility = _utility_land(pool, chosen, colors) if not splash else None
    if utility is not None:
        nonbasic.append(utility)
    kept = Counter(card["grp_id"] for card in chosen + nonbasic)
    sources = Counter(color for card in nonbasic for color in _produces(card) & set(colors))
    fixers = Counter(color for card in chosen for color in _produces(card) & set(splash))
    basics: Counter = Counter()
    for color in splash:
        basics[color] = max(0 if sources[color] else 1, 3 - sources[color] - fixers[color])
    remaining = land_slots - len(nonbasic) - sum(basics.values())
    weights = Counter({color: 0 for color in main})
    for card in chosen:
        for options in _symbols(card):
            payable = options & set(main)
            for color in payable:
                weights[color] += 1 / len(payable)
    total_weight = sum(weights.values())
    first = round(remaining * weights[main[0]] / total_weight) if total_weight else remaining // 2
    if all(weights[color] for color in main):
        first = min(remaining - max(0, 6 - sources[main[1]]), max(6 - sources[main[0]], first))
    basics[main[0]] += first
    basics[main[1]] += remaining - first
    in_deck = {card["grp_id"] for card in chosen + nonbasic}
    cuts = []
    for card in _nonbasics(pool):
        if card["grp_id"] in in_deck and kept[card["grp_id"]] >= sum(
            c["grp_id"] == card["grp_id"] for c in _nonbasics(pool)
        ):
            continue
        if "land" in card.get("type_line", "").lower():
            reason = "basics make this deck's colors more reliably than this land"
        elif not _castable(card, colors):
            reason = "outside the chosen colors without a supported mana base"
        elif normalize_card(card).cmc >= 5:
            reason = "reduce expensive cards and preserve earlier plays"
        else:
            reason = "keep the stronger on-color creatures and interaction in this starting build"
        cuts.append({"grp_id": card["grp_id"], "reason": reason})
    result = validate_deck(
        {
            "main_deck": [{"grp_id": grp_id, "count": count} for grp_id, count in kept.items()],
            "basic_lands": {color: count for color, count in basics.items() if count},
            "cuts": cuts,
            "plan": _plan_text(main, candidate["quality"], splash),
        },
        pool,
        source="heuristic",
    )
    result["quality"] = candidate["quality"]
    return result


def _plan_text(colors: tuple[str, ...], quality: dict, splash: tuple[str, ...] = ()) -> str:
    names = "/".join(COLOR_NAMES[color] for color in colors)
    if splash and quality.get("splash_cards"):
        names += f" splashing {COLOR_NAMES[splash[0]]} for {', '.join(quality['splash_cards'][:3])}"
    head = f"{names} {quality['archetype']}" if quality.get("archetype") else names
    parts = [f"{head}: avg GIH {quality['avg_gih']}%" if quality.get("avg_gih") else head]
    parts.append(f"{quality['creatures']} creatures, {quality['removal']} removal")
    if quality.get("bombs"):
        parts.append("bombs " + ", ".join(quality["bombs"][:3]))
    return "; ".join(parts) + "."


def score_deck(
    main_deck: list[dict],
    pool: list[dict],
    pair_win_rates: dict | None = None,
    *,
    fmt: str = "draft",
    strategy: DeckStrategy | None = None,
) -> dict:
    """deck_quality for a validated deck given as [{grp_id, count}], splash included."""
    by_id = {card["grp_id"]: card for card in _nonbasics(pool)}
    chosen, lands = [], []
    for entry in main_deck:
        card = by_id.get(entry["grp_id"])
        if card is not None:
            target = lands if "land" in card.get("type_line", "").lower() else chosen
            target += [card] * int(entry.get("count") or 1)
    main, splash = _color_roles(chosen)
    fixing = sum(bool(_produces(card) & set(splash)) for card in lands + chosen)
    return deck_quality(
        chosen, main + splash, pair_win_rates, fmt=fmt, splash=splash, fixing=fixing, strategy=strategy
    )


# Whether the builder's valuation counts rules-text removal (shrink, edict,
# fight) or only the older keyword tags; narration and the deck-strength
# model always use the rules-text detector. A benchmark knob.
BUILDER_REMOVAL_RULES = True


def _builder_removal(card: dict) -> bool:
    if BUILDER_REMOVAL_RULES:
        return _interaction_kind(card) == "removal"
    return "removal" in normalize_card(card).tags


def _interaction_kind(card: dict) -> str | None:
    """Removal deals with a permanent for good; tempo (bounce, tap, counters) buys time."""
    text = html.unescape(re.sub(r"<[^>]*>", "", card.get("oracle_text", ""))).lower()
    from arenamcp.draft_autopick import is_removal

    # 2026-10-06 UB draft: Last Gasp (-3/-3) and Break Under Pressure (edict)
    # read as non-removal here, so both Last Gasps sat in the sideboard. The
    # draft ranker's rules-text detector covers shrink, edict and fight effects.
    if is_removal(SimpleNamespace(oracle=card.get("oracle_text", ""))):
        return "removal"
    if "removal" in normalize_card(card).tags or (
        "target" in text and "library" in text and ("bottom" in text or "shuffles it into" in text)
    ):
        return "removal"
    if (
        "counter target" in text
        or ("return target" in text and "hand" in text)
        or "tap target creature" in text
        or re.search(r"target creature gets -\d+/-0", text)
    ):
        return "tempo"
    return None


def _deck_cards(build: dict, pool: list[dict]) -> list[dict]:
    by_id = {card["grp_id"]: card for card in _nonbasics(pool)}
    return [
        by_id[entry["grp_id"]]
        for entry in build.get("main_deck", [])
        if entry["grp_id"] in by_id and "land" not in by_id[entry["grp_id"]].get("type_line", "").lower()
        for _ in range(entry["count"])
    ]


def _deck_profile(build: dict, pool: list[dict]) -> dict | None:
    """What the narration may claim, counted from the deck's actual cards and rules."""
    chosen = _deck_cards(build, pool)
    if not chosen:
        return None
    bodies = [card for card in chosen if rules_profile(card)["unconditional_body"]]
    kinds = Counter(_interaction_kind(card) for card in chosen)
    leaders = list(
        dict.fromkeys(card.get("name", "") for card in sorted(bodies, key=_card_value, reverse=True))
    )
    main, splash = _color_roles(chosen)
    colors = list(main + splash)
    color_text = "-".join(COLOR_NAMES[c] for c in main) or "colorless"
    if splash:
        splashed = list(
            dict.fromkeys(card["name"] for card in chosen if _required_colors(card) & set(splash))
        )
        color_text += (
            f" splashing {' and '.join(COLOR_NAMES[c] for c in splash)} for {' and '.join(splashed[:2])}"
        )
    return {
        "cards": chosen,
        "colors": colors,
        "color_text": color_text,
        "bodies": len(bodies),
        "early_bodies": sum(normalize_card(card).cmc <= 3 for card in bodies),
        "evasive": [card["name"] for card in bodies if "evasion" in normalize_card(card).tags],
        "removal": kinds["removal"],
        "tempo": kinds["tempo"],
        "card_advantage": sum("card_advantage" in normalize_card(card).tags for card in chosen),
        "top_end": sum(normalize_card(card).cmc >= 5 for card in chosen),
        "leaders": [name for name in leaders if name][:2],
        "quality": build.get("quality") or deck_quality(chosen, tuple(colors)),
    }


# A model "plan" about assembling the deck is not a plan for winning games
# (2026-10-06: "Play the top-scoring pure UG build ... keep the mana base legal").
_BUILD_TALK = re.compile(r"\b(build|mana base|legal|splash|top-scoring|score|candidate|counted)\b", re.I)


def _one_clause(text: str, max_words: int = 25) -> str:
    """A model plan reduced to one clause so the spoken summary stays two sentences."""
    first = re.split(r"(?<=[.!?])\s", " ".join(str(text).split()), maxsplit=1)[0]
    words = first.replace(";", ",").replace(":", ",").split()[:max_words]
    return " ".join(words).rstrip(".!?,; ")


def _concerns(profile: dict) -> list[str]:
    concerns = []
    if profile["bodies"] < 13:
        concerns.append(f"only {profile['bodies']} creatures")
    if profile["early_bodies"] < 5:
        concerns.append(f"only {profile['early_bodies']} creatures costing three or less")
    if profile["removal"] < 3:
        concerns.append(f"only {_count(profile['removal'], 'removal spell')} for opposing bombs")
    if profile["top_end"] >= 5:
        concerns.append(f"{profile['top_end']} cards costing five or more")
    return concerns[:2]


def deck_choice_summary(build: dict, pool: list[dict]) -> str:
    """Two spoken sentences grounded in the deck's actual cards, never a win-rate forecast."""
    profile = _deck_profile(build, pool)
    if profile is None:
        return "Deck submitted. I couldn't verify enough card details to explain its strategy."
    featured = "led by " + " and ".join(profile["leaders"]) if profile["leaders"] else ""
    interaction = _count(profile["removal"], "removal spell")
    if profile["tempo"]:
        interaction += " and " + _count(profile["tempo"], "bounce, tap, or counter spell")
    first = (
        f"I built {profile['color_text']}{', ' + featured if featured else ''}, with {profile['bodies']} creature or token spells, "
        f"{profile['early_bodies']} of them costing three or less, and {interaction}."
    )
    model_plan = _one_clause(build.get("plan") or "") if build.get("reasoning_source") == "card_rules" else ""
    if model_plan and not _BUILD_TALK.search(model_plan):
        plan = model_plan
    elif len(profile["evasive"]) >= 4:
        plan = "win with evasive threats while trading on the ground"
    elif profile["early_bodies"] >= 7:
        plan = "curve out with early creatures and keep attacking"
    elif profile["card_advantage"] >= 4 and profile["removal"] + profile["tempo"] >= 4:
        plan = "trade early, answer threats, and win the long game on card advantage"
    elif profile["bodies"]:
        plan = "build a board and win through creature combat"
    else:
        plan = "lean on spells, though it lacks reliable creatures to finish games"
    concerns = _concerns(profile)
    risk = (
        f"but its weak point{'s are' if len(concerns) > 1 else ' is'} " + " and ".join(concerns)
        if concerns
        else "with no clear gap in creatures, curve, or interaction"
    )
    lead = "Its plan: " if plan == model_plan else "Its plan is to "
    return f"{first} {lead}{plan}, {risk}."


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def reconcile_logged_deck(build: dict) -> dict:
    """Describe changes against the last observed deck, never assume unsaved edits."""
    current = build.get("editor_cards")
    if current is None or build.get("total_cards") != 40:
        return build
    basic_types = {"W": "plains", "U": "island", "B": "swamp", "R": "mountain", "G": "forest"}
    desired = Counter({entry["grp_id"]: entry["count"] for entry in build["main_deck"]})
    desired.update({color: count for color, count in build["basic_lands"].items()})
    observed = Counter()
    names = {entry["grp_id"]: entry["name"] for entry in build["main_deck"]}
    names.update(BASIC_NAMES)
    for card in current:
        kind = str(card.get("type_line") or "").lower()
        key = next(
            (color for color, land_type in basic_types.items() if "basic land" in kind and land_type in kind),
            card["grp_id"],
        )
        observed[key] += card["count"]
        if isinstance(key, int):
            names[key] = card.get("name") or f"Card {key}"
    remove, add = observed - desired, desired - observed
    live = build.get("editor_basis") == "live_editor"
    text = f"Your {'current' if live else 'last logged'} deck has {sum(observed.values())} cards. "
    if remove:
        reasons = {entry["grp_id"]: entry["reason"] for entry in build["cuts"]}
        text += (
            "Cut "
            + "; ".join(
                f"{count} {names[key]}" + (f" — {reasons[key]}" if key in reasons else "")
                for key, count in remove.items()
            )
            + ". "
        )
    if add:
        text += "Add " + "; ".join(f"{count} {names[key]}" for key, count in add.items()) + ". "
    if not remove and not add:
        text += "It already matches the recommended 40-card build. "
    else:
        text += "Those changes make exactly 40 cards. "
    if not live:
        text += "Unsaved editor changes may not be logged yet."
    return {
        **build,
        "basis": "live_editor" if live else "logged_deck",
        "remaining_cuts": dict(remove),
        "remaining_additions": dict(add),
        "spoken_advice": text,
        "detailed_text": text + "\n" + build["detailed_text"],
    }
